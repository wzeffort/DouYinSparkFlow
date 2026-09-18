from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from spark_console.db import session_scope
from spark_console.models import DouyinAccount
from spark_console.services import NotFound, ValidationError
from spark_console.services.manual_retries import ManualRetryService
from spark_console.services.recipient_review import RecipientReviewService, display_evidence


def build_recipient_review_router(engine, auth, page, task_write_lock):
    router = APIRouter()

    @router.get('/runs/{run_id}/recipients/{position}/review')
    def review(request: Request, run_id: str, position: int):
        with session_scope(engine) as db:
            actor, _record, context = auth.user_context(request, db)
            try:
                run, task, row, evidence, approval, digest = RecipientReviewService(db).load(actor, run_id, position)
            except NotFound:
                raise HTTPException(404) from None
            notice = {
                'approved': '人工确认已保存，后续遇到同一名称差异可以继续发送。',
                'revoked': '已撤销；下次执行起不再使用此确认。',
                'scheduled': '已安排仅补跑这位明确未发送的好友，其他好友不会重发。',
            }.get(request.query_params.get('notice'))
            return page(request, 'recipient_review.html', title='好友名称诊断与确认', run=run, task=task,
                item=row, evidence=display_evidence(evidence), approval=approval,
                account=db.get(DouyinAccount, row.account_id),
                evidence_digest=digest, notice=notice, **context)

    @router.post('/runs/{run_id}/recipients/{position}/review')
    def update(request: Request, run_id: str, position: int, csrf_token: str = Form(default=''),
               action: str = Form(default=''), evidence_digest: str = Form(default=''),
               confirm_same_person: str = Form(default='')):
        with task_write_lock, session_scope(engine) as db:
            if engine.dialect.name == 'sqlite':
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            actor, record, context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            service = RecipientReviewService(db)
            try:
                service.load(actor, run_id, position)
                if action == 'approve':
                    if confirm_same_person != 'yes':
                        raise ValidationError('请先核对诊断并勾选“确认为同一好友”')
                    service.approve(actor, run_id, position, evidence_digest)
                    notice = 'approved'
                elif action == 'revoke':
                    service.revoke(actor, run_id, position)
                    notice = 'revoked'
                elif action == 'retry':
                    result = ManualRetryService(db).schedule(actor.id, [run_id], owned=True, positions=[position])
                    if not result or not result[0]['scheduled_for']:
                        raise ValidationError(result[0]['reason'] if result else '无法安排重试')
                    notice = 'scheduled'
                else:
                    raise ValidationError('操作无效')
            except NotFound:
                raise HTTPException(404) from None
            except ValidationError as error:
                return page(request, 'error.html', status_code=409, title='操作未完成', message=str(error), **context)
        return RedirectResponse(f'/runs/{run_id}/recipients/{position}/review?notice={notice}', status_code=303)

    return router
