from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from spark_console.db import session_scope
from spark_console.models import DouyinAccount
from spark_console.services import Conflict
from spark_console.services.contacts import ContactSyncService


def build_contact_router(engine, auth):
    router = APIRouter()

    def authorize(request, db, account_id, csrf_token=None):
        user, record = auth.current(request, db)
        if csrf_token is not None:
            auth.csrf(record, csrf_token)
        account = db.get(DouyinAccount, account_id)
        if account is None or (account.owner_user_id != user.id and user.role != 'admin'):
            raise HTTPException(404)
        return account

    @router.get('/accounts/{account_id}/contact-sync')
    def status(request: Request, account_id: str):
        with session_scope(engine) as db:
            authorize(request, db, account_id)
            return JSONResponse(ContactSyncService(db).status(account_id), headers={'Cache-Control':'no-store'})

    @router.post('/accounts/{account_id}/contact-sync')
    def start(request: Request, account_id: str, csrf_token: str = Form(default='')):
        with session_scope(engine) as db:
            account = authorize(request, db, account_id, csrf_token)
            try:
                result = ContactSyncService(db).request(account)
            except Conflict as error:
                return JSONResponse({'message':str(error)},status_code=409)
        return JSONResponse(result,status_code=202,headers={'Cache-Control':'no-store'})

    @router.post('/accounts/{account_id}/contact-sync/cancel')
    def cancel(request: Request, account_id: str, csrf_token: str = Form(default='')):
        with session_scope(engine) as db:
            authorize(request, db, account_id, csrf_token)
            result = ContactSyncService(db).cancel(account_id)
        return JSONResponse(result,headers={'Cache-Control':'no-store'})

    return router
