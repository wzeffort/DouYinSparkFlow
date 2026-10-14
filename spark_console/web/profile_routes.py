from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from spark_console.db import session_scope
from spark_console.models import EmailVerificationRequest
from spark_console.pii import mask_email
from spark_console.profile_models import EmailBindingProof, UserProfile
from spark_console.services import Conflict, ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.email_verification import EmailVerificationService
from spark_console.services.profiles import ProfileService, aware


def build_profile_router(engine, auth, passwords, pii, page):
    router = APIRouter()

    def profiles(db):
        return ProfileService(db, passwords, AuditService(db))

    def emails(db):
        if pii is None:
            raise ValidationError('邮箱验证服务暂未开启，请联系管理员')
        return EmailVerificationService(db, passwords, pii, AuditService(db))

    def render(request, db, user, record, error=None, verification_id=None, section=None):
        profile = db.get(UserProfile, user.id)
        email = emails(db).email_for_user(user) if pii else None
        request_id = verification_id or request.query_params.get('verification', '')[:36]
        pending = db.get(EmailVerificationRequest, request_id) if request_id else None
        now = datetime.now(timezone.utc)
        if pending and (not pii or pending.user_id != user.id or pending.purpose != 'bind' or
                        pending.consumed_at is not None or aware(pending.code_expires_at) <= now):
            pending = None
        new_email_masked, requires_old = None, False
        if pending:
            new_email_masked = mask_email(pii.decrypt_email(pending.email_ciphertext, pending.email_nonce,
                aad=f'email-request:{pending.id}'.encode()))
            proof = db.get(EmailBindingProof, pending.id)
            requires_old = bool(proof and proof.old_code_hash)
        next_change = aware(profile.username_changed_at) + ProfileService.USERNAME_COOLDOWN if profile and profile.username_changed_at else None
        return page(request, 'profile.html', 400 if error else 200, title='个人中心',
            profile=profile, email_masked=mask_email(email) if email else None,
            email_verified=bool(user.email_verified_at), pending=pending,
            new_email_masked=new_email_masked, requires_old=requires_old,
            next_username_change=next_change if next_change and next_change > now else None,
            error=error, error_section=section, notice=request.query_params.get('notice'),
            **auth._page_context(db, user, record))

    def sign_in_again(reason):
        response = RedirectResponse('/login?' + reason + '=1', 303)
        response.delete_cookie('spark_session')
        return response

    @router.get('/profile')
    def profile_page(request: Request):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            return render(request, db, user, record)

    @router.post('/profile/info')
    def save_info(request: Request, csrf_token: str = Form(''), nickname: str = Form(''),
                  age: str = Form(''), gender: str = Form('private'), bio: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            try:
                profiles(db).save_info(user, nickname, age, gender, bio)
            except ValidationError as exc:
                return render(request, db, user, record, str(exc), section='info')
        return RedirectResponse('/profile?notice=info-saved#info', 303)

    @router.post('/profile/username')
    def change_username(request: Request, csrf_token: str = Form(''), username: str = Form(''),
                        current_password: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            try:
                changed = profiles(db).change_username(user, username, current_password)
            except (ValidationError, Conflict) as exc:
                return render(request, db, user, record, str(exc), section='account')
        return sign_in_again('account_changed') if changed else RedirectResponse('/profile?notice=unchanged#account', 303)

    @router.post('/profile/password')
    def change_password(request: Request, csrf_token: str = Form(''), current_password: str = Form(''),
                        new_password: str = Form(''), new_password_confirmation: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            try:
                profiles(db).change_password(user, current_password, new_password, new_password_confirmation)
            except ValidationError as exc:
                return render(request, db, user, record, str(exc), section='password')
        return sign_in_again('password_reset')

    @router.get('/profile/avatar')
    def avatar(request: Request):
        with session_scope(engine) as db:
            user, _ = auth.current(request, db)
            profile = db.get(UserProfile, user.id)
            if not profile or not profile.avatar_image:
                raise HTTPException(404, '尚未设置头像')
            return Response(profile.avatar_image, media_type='image/jpeg', headers={
                'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})

    @router.post('/profile/avatar')
    def upload_avatar(request: Request, csrf_token: str = Form(''), avatar: UploadFile = File(...)):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            try:
                profiles(db).save_avatar(user, avatar.file.read(ProfileService.MAX_AVATAR_BYTES + 1))
            except ValidationError as exc:
                return render(request, db, user, record, str(exc), section='avatar')
        return RedirectResponse('/profile?notice=avatar-saved#info', 303)

    @router.post('/profile/avatar/remove')
    def remove_avatar(request: Request, csrf_token: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            profiles(db).remove_avatar(user)
        return RedirectResponse('/profile?notice=avatar-removed#info', 303)

    @router.post('/profile/email/start')
    def start_email(request: Request, csrf_token: str = Form(''), email: str = Form(''),
                    current_password: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            try:
                pending = emails(db).start_binding(user.id, email, current_password=current_password)
            except (ValidationError, Conflict, ValueError) as exc:
                return render(request, db, user, record,
                    '请输入正确的邮箱地址' if isinstance(exc, ValueError) else str(exc), section='email')
        return RedirectResponse('/profile?verification=' + pending.id + '#email', 303)

    @router.post('/profile/email/verify/{request_id}')
    def verify_email(request: Request, request_id: str, csrf_token: str = Form(''),
                     code: str = Form(''), old_code: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            replacing = user.email_verified_at is not None
            try:
                emails(db).verify_binding(user.id, request_id, code, old_code=old_code)
            except (ValidationError, Conflict, ValueError) as exc:
                return render(request, db, user, record, str(exc), verification_id=request_id, section='email')
        return sign_in_again('email_changed') if replacing else RedirectResponse('/profile?notice=email-bound#email', 303)

    return router
