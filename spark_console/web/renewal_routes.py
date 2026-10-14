from io import BytesIO
from PIL import Image, UnidentifiedImageError
from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, File, Query
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, case, func
from spark_console.db import session_scope
from spark_console.models import User
from spark_console.renewal_models import RenewalOrder, RenewalPaymentCode
from spark_console.services import ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.notifications import NotificationService
from spark_console.services.renewals import CHANNELS, PLANS, RenewalService, RenewalSubmissionLimited
from spark_console.services.task_capacity import TaskCapacityService


def build_renewal_router(engine, auth, pii, page):
    router = APIRouter()

    def service(db):
        audit = AuditService(db)
        return RenewalService(db, audit, NotificationService(db, pii, audit) if pii else None)

    def invoke(fn):
        try: return fn()
        except RenewalSubmissionLimited as error:
            raise HTTPException(429, str(error), headers={'Retry-After':str(error.retry_after)}) from error
        except ValidationError as error: raise HTTPException(400, str(error)) from error

    @router.get('/renewal')
    def renewal(request: Request, grant: str = ''):
        with session_scope(engine) as db:
            user, record, context = auth.user_context(request, db)
            renewals = service(db)
            if grant: invoke(lambda: renewals._target(user.id, grant))
            grants = [g for g in TaskCapacityService(db, AuditService(db)).grants_for(user.id)
                      if g.amount == 1 and g.revoked_at is None and g.expires_at is not None] if user.role != 'admin' else []
            orders = db.scalars(select(RenewalOrder).where(RenewalOrder.user_id == user.id)
                                .order_by(RenewalOrder.created_at.desc()).limit(20)).all()
            return page(request, 'renewal.html', title='我的续卡', plans=renewals.catalog(user.id),
                        referral_count=renewals.referral_count(user.id), selected_grant=grant,
                        grants=grants, orders=orders, labels=PLANS, **context)

    @router.post('/renewal/orders')
    def create_order(request: Request, csrf_token: str = Form(''), plan: str = Form(''),
                     channel: str = Form(''), grant: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            order = invoke(lambda: service(db).create(user, plan, channel, grant or None))
            return RedirectResponse('/renewal/orders/'+order.id, 303)

    @router.get('/renewal/orders/{order_id}')
    def checkout(request: Request, order_id: str):
        with session_scope(engine) as db:
            user, record, context = auth.user_context(request, db)
            order = invoke(lambda: service(db).owned(user, order_id))
            code = db.get(RenewalPaymentCode, order.channel)
            return page(request, 'renewal_checkout.html', title='续卡付款', order=order,
                        plan_label=PLANS[order.plan_key][0], channel_label=CHANNELS[order.channel],
                        amount=f'{order.price_cents/100:.2f}', code_ready=bool(code), **context)

    @router.post('/renewal/orders/{order_id}/submitted')
    def report_payment(request: Request, order_id: str, csrf_token: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            renewals = service(db)
            try:
                invoke(lambda: renewals.submit(user, order_id))
            except HTTPException as error:
                if error.status_code != 429: raise
                _, _, context = auth.user_context(request, db)
                order = renewals.owned(user, order_id)
                response = page(request, 'renewal_checkout.html', title='续卡付款',
                    order=order, plan_label=PLANS[order.plan_key][0],
                    channel_label=CHANNELS[order.channel], amount=f'{order.price_cents/100:.2f}',
                    code_ready=bool(db.get(RenewalPaymentCode, order.channel)),
                    submission_error=error.detail, status_code=429, **context)
                response.headers.update(error.headers or {})
                return response
            return RedirectResponse('/renewal/orders/'+order_id, 303)

    @router.post('/renewal/orders/{order_id}/cancel')
    def cancel_order(request: Request, order_id: str, csrf_token: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record = auth.current(request, db); auth.csrf(record, csrf_token)
            invoke(lambda: service(db).cancel(user, order_id))
            return RedirectResponse('/renewal/orders/'+order_id, 303)

    @router.get('/renewal/codes/{channel}')
    def payment_code(request: Request, channel: str):
        with session_scope(engine) as db:
            auth.current(request, db)
            code = db.get(RenewalPaymentCode, channel)
            if not code: raise HTTPException(404, '收款码尚未配置')
            return Response(code.image, media_type=code.content_type, headers={'Cache-Control':'private, no-store'})

    @router.get('/admin/renewals')
    def admin_renewals(request: Request, page_number: int = Query(1, ge=1, le=100000), order: str = ''):
        with session_scope(engine) as db:
            admin, record, context = auth.admin_context(request, db)
            query = select(RenewalOrder, User).join(User, User.id == RenewalOrder.user_id)
            if order: query = query.where(RenewalOrder.id == order[:36])
            total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
            rows = db.execute(query.order_by(case((RenewalOrder.status == 'submitted', 0), else_=1),
                                            RenewalOrder.created_at.desc())
                              .offset((page_number-1)*100).limit(100)).all()
            return page(request, 'admin_renewals.html', title='续卡订单与收款码', rows=rows,
                        codes={c:bool(db.get(RenewalPaymentCode,c)) for c in CHANNELS},
                        channels=CHANNELS, labels=PLANS, page_number=page_number,
                        has_next=page_number*100<total, searched_order=order, **context)

    @router.post('/admin/renewals/{order_id}/approve')
    def approve(request: Request, order_id: str, csrf_token: str = Form(''), checked: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            admin, record, _ = auth.admin_context(request, db); auth.csrf(record, csrf_token)
            if checked != 'yes': raise HTTPException(400, '请先核对金额和支付记录')
            invoke(lambda: service(db).approve(admin, order_id))
            return RedirectResponse('/admin/renewals',303)

    @router.post('/admin/renewals/{order_id}/reject')
    def reject(request: Request, order_id: str, csrf_token: str = Form('')):
        with auth.mutation_lock, session_scope(engine) as db:
            admin, record, _ = auth.admin_context(request, db); auth.csrf(record, csrf_token)
            invoke(lambda: service(db).reject(admin, order_id))
            return RedirectResponse('/admin/renewals',303)

    @router.post('/admin/renewals/codes/{channel}')
    def upload_code(request: Request, channel: str, csrf_token: str = Form(''), image: UploadFile = File(...)):
        with auth.mutation_lock, session_scope(engine) as db:
            admin, record, _ = auth.admin_context(request, db); auth.csrf(record, csrf_token)
            if channel not in CHANNELS: raise HTTPException(404)
            data = image.file.read(2*1024*1024+1)
            if len(data)>2*1024*1024: raise HTTPException(400, '收款码图片不能超过 2 MB')
            try:
                with Image.open(BytesIO(data)) as picture:
                    if picture.format not in ('PNG','JPEG') or picture.width*picture.height>16000000:
                        raise ValueError('invalid image')
                    picture.load()
                    clean=BytesIO(); picture.convert('RGB').save(clean,format='PNG')
                    data=clean.getvalue()
                    if len(data)>2*1024*1024: raise ValueError('image too large')
            except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
                raise HTTPException(400, '请上传有效的 PNG 或 JPG 收款码图片') from None
            content_type = 'image/png'
            code=db.get(RenewalPaymentCode,channel)
            if code: code.image=data; code.content_type=content_type
            else: db.add(RenewalPaymentCode(channel=channel,image=data,content_type=content_type))
            AuditService(db).write(admin.id,'renewal.payment_code_updated','renewal_payment_code',channel)
            return RedirectResponse('/admin/renewals',303)

    return router
