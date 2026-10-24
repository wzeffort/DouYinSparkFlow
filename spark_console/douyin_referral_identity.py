"""Only the authenticated scanner may supply these identities, never web forms."""
import re
from sqlalchemy import delete, update
from spark_console.referral_models import AccountReferralIdentity, ReferralLock, VerifiedDouyin

ACCOUNT_INFO_URL = "https://www.douyin.com/passport/account/info/v2/?aid=6383"


def canonical_user_id(value):
    if type(value) not in (str, int):
        return None
    value = str(value)
    return value if re.fullmatch(r"[1-9][0-9]{0,31}", value) else None


async def read_verified_user_id(context):
    try:
        response = await context.request.get(ACCOUNT_INFO_URL, timeout=10_000)
        if response.status != 200:
            return None
        body = await response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict) and body.get("message") == "success" and data.get("error_code") == 0:
            return canonical_user_id(data.get("user_id"))
    except Exception:
        pass
    return None


def record_verified_binding(db, account, platform_user_id):
    result = db.execute(update(ReferralLock).where(ReferralLock.id == 1).values(version=ReferralLock.version + 1))
    if result.rowcount != 1:
        raise RuntimeError("Referral schema has not been initialized")
    # A newly scanned credential without an identity must not inherit the old identity.
    db.execute(delete(AccountReferralIdentity).where(AccountReferralIdentity.account_id == account.id))
    key = canonical_user_id(platform_user_id)
    if key is None:
        return
    if db.get(VerifiedDouyin, key) is None:
        db.add(VerifiedDouyin(platform_user_id=key, first_user_id=account.owner_user_id))
    db.add(AccountReferralIdentity(account_id=account.id, platform_user_id=key))
    db.flush()
