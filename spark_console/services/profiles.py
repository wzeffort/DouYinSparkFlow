from datetime import timedelta, timezone
from io import BytesIO
import re
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select, update

from spark_console.models import EmailVerificationRequest, User, WebSession, utc_now, uuid_string
from spark_console.profile_models import UserProfile
from spark_console.services import Conflict, ValidationError
from spark_console.services.users import validate_registration_username


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class ProfileService:
    USERNAME_COOLDOWN = timedelta(days=7)
    MAX_AVATAR_BYTES = 2 * 1024 * 1024

    def __init__(self, session, passwords, audit):
        self.session, self.passwords, self.audit = session, passwords, audit

    def lock_user(self, user):
        changed = self.session.execute(update(User).where(User.id == user.id).values(status=User.status))
        if not changed.rowcount:
            raise ValidationError('账号不存在，请重新登录')
        self.session.refresh(user)
        if user.status != 'active':
            raise ValidationError('账号已停用')
        return user

    def profile(self, user):
        profile = self.session.get(UserProfile, user.id)
        if profile is None:
            profile = UserProfile(user_id=user.id)
            self.session.add(profile)
            self.session.flush()
        else:
            self.session.refresh(profile)
        return profile

    def verify_current_password(self, user, password, now=None):
        self.lock_user(user)
        current = now or utc_now()
        profile = self.profile(user)
        if profile.security_locked_until and aware(profile.security_locked_until) > aware(current):
            raise ValidationError('当前密码验证失败次数过多，请 15 分钟后再试')
        if profile.security_locked_until:
            profile.security_locked_until = None
            profile.security_failed_attempts = 0
        if len(password) > 1024 or not self.passwords.verify(user.password_hash, password):
            profile.security_failed_attempts += 1
            if profile.security_failed_attempts >= 5:
                profile.security_locked_until = current + timedelta(minutes=15)
            raise ValidationError('当前密码不正确')
        profile.security_failed_attempts = 0
        profile.security_locked_until = None

    def save_info(self, user, nickname, age, gender, bio):
        nickname, age, bio = nickname.strip(), age.strip(), bio.strip()
        if len(nickname) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in nickname):
            raise ValidationError('昵称最多 32 个字，请不要使用控制字符')
        if age and (not re.fullmatch(r'[0-9]{1,3}', age) or not 0 <= int(age) <= 120):
            raise ValidationError('年龄请填写 0–120 的整数，或留空')
        if gender not in {'private', 'female', 'male', 'other'}:
            raise ValidationError('请选择有效的性别选项')
        if len(bio) > 160:
            raise ValidationError('个人简介最多 160 个字')
        self.lock_user(user)
        profile = self.profile(user)
        profile.nickname, profile.age = nickname or None, int(age) if age else None
        profile.gender, profile.bio = gender, bio or None
        self.audit.write(user.id, 'profile.updated', 'user', user.id)
        return profile

    def revoke_security_requests(self, user, now):
        self.session.query(WebSession).filter(WebSession.user_id == user.id).delete(synchronize_session=False)
        self.session.query(EmailVerificationRequest).filter(
            EmailVerificationRequest.user_id == user.id,
            EmailVerificationRequest.consumed_at.is_(None)).update(
                {EmailVerificationRequest.consumed_at: now}, synchronize_session=False)

    def change_username(self, user, username, current_password, now=None):
        name = validate_registration_username(username)
        current = now or utc_now()
        self.verify_current_password(user, current_password, current)
        if name == user.username:
            return False
        profile = self.profile(user)
        if profile.username_changed_at and aware(profile.username_changed_at) + self.USERNAME_COOLDOWN > aware(current):
            raise ValidationError('账号每 7 天只能修改一次，请到下次可修改时间后再试')
        if self.session.scalar(select(User.id).where(User.username == name, User.id != user.id)):
            raise Conflict('该账号已被使用，请换一个')
        user.username = name
        profile.username_changed_at = current
        self.revoke_security_requests(user, current)
        self.audit.write(user.id, 'profile.username_changed', 'user', user.id)
        self.session.flush()
        return True

    def change_password(self, user, current_password, password, confirmation):
        if password != confirmation:
            raise ValidationError('两次输入的新密码不一致')
        if not 9 <= len(password) <= 128:
            raise ValidationError('新密码需要 9–128 位')
        self.verify_current_password(user, current_password)
        if self.passwords.verify(user.password_hash, password):
            raise ValidationError('新密码不能与当前密码相同')
        user.password_hash = self.passwords.hash(password)
        user.must_change_password = False
        user.failed_login_count, user.locked_until = 0, None
        self.revoke_security_requests(user, utc_now())
        self.audit.write(user.id, 'profile.password_changed', 'user', user.id)

    def save_avatar(self, user, data):
        if not data or len(data) > self.MAX_AVATAR_BYTES:
            raise ValidationError('头像图片不能超过 2 MB')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as picture:
                    if picture.format not in {'PNG', 'JPEG', 'WEBP'} or picture.width * picture.height > 12_000_000 or getattr(picture, 'is_animated', False):
                        raise ValueError('invalid image')
                    picture.load()
                    oriented = ImageOps.exif_transpose(picture)
                    image = ImageOps.fit(oriented.convert('RGBA'), (512, 512), method=Image.Resampling.LANCZOS)
                    background = Image.new('RGB', image.size, '#fff4ef')
                    background.paste(image, mask=image.getchannel('A'))
                    output = BytesIO(); background.save(output, format='JPEG', quality=88)
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise ValidationError('请上传有效的静态 PNG、JPG 或 WebP 图片，最多 1200 万像素') from None
        self.lock_user(user)
        profile = self.profile(user)
        profile.avatar_image, profile.avatar_version = output.getvalue(), uuid_string()
        self.audit.write(user.id, 'profile.avatar_changed', 'user', user.id)

    def remove_avatar(self, user):
        self.lock_user(user)
        profile = self.profile(user)
        profile.avatar_image, profile.avatar_version = None, None
        self.audit.write(user.id, 'profile.avatar_removed', 'user', user.id)
