import hashlib
import json

from fastapi import APIRouter, Form, HTTPException, Request

from spark_console.db import session_scope
from spark_console.services.admin_operations import AdminOperations
from spark_console.services.manual_retries import ManualRetryService


def scope(day, run_ids):
    digest = hashlib.sha256(json.dumps(sorted(set(run_ids)), separators=(',', ':')).encode()).hexdigest()
    return f'runs.retry:{day}:{digest}'


def build_retry_router(engine, auth, page, task_write_lock):
    router = APIRouter()

    @router.get('/admin/runs/retry-today')
    def preview(request: Request):
        with session_scope(engine) as db:
            admin, record, context = auth.admin_context(request, db)
            service = ManualRetryService(db)
            entries = service.preview(admin.id)
            run_ids = [entry['run_id'] for entry in entries if entry['eligible']]
            token = AdminOperations(db, record).issue(scope(service.day, run_ids))
            return page(request, 'retry_today.html', title='重跑今日失败', entries=entries, day=service.day,
                run_ids=json.dumps(run_ids), operation_token=token, result=False, **context)

    @router.post('/admin/runs/retry-today')
    def confirm(request: Request, day: str = Form(default=''), run_ids: str = Form(default=''),
                operation_token: str = Form(default=''), csrf_token: str = Form(default='')):
        if len(run_ids) > 100000:
            raise HTTPException(400, '重跑列表过长')
        try:
            ids = json.loads(run_ids)
            if not isinstance(ids, list) or any(not isinstance(value, str) or len(value) != 36 for value in ids):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, '重跑列表无效') from None
        with task_write_lock, session_scope(engine) as db:
            # All deployed scheduling writers use SQLite transactions; reserve
            # the write lock before inspecting a preview that may have gone stale.
            if engine.dialect.name == 'sqlite':
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            admin, record, context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            service = ManualRetryService(db)
            if day != service.day:
                raise HTTPException(409, '日期已变化，请重新查看今日重跑列表')
            entries = []
            executed = AdminOperations(db, record).execute(operation_token, scope(day, ids),
                {'day': day, 'run_ids': sorted(set(ids))}, lambda: entries.extend(service.schedule(admin.id, ids)))
            if not executed:
                entries = [entry for entry in service.preview(admin.id) if entry['run_id'] in ids]
            return page(request, 'retry_today.html', title='重跑安排结果', entries=entries, day=day,
                result=True, already_submitted=not executed, **context)

    return router
