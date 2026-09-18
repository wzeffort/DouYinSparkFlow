"""Per-account, per-recipient evidence and explicitly approved display-name pairs."""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select

from spark_console.models import (
    DouyinAccount, RecipientCheckEvidence, RecipientNameApproval, SparkTask,
    SparkTaskRecipient, TaskRun, TaskRunRecipient,
)
from spark_console.services import NotFound, ValidationError
from spark_console.services.audits import AuditService

REASONS = {
    'name_difference_allowed': '已选中任务好友；标题名称差异仅记录，不阻止发送',
    'matched': '名称一致（含 Unicode 标准等价形式）',
    'human_approved': '已使用人工确认的名称对应关系',
    'name_mismatch': '已读取标题，但文字不同',
    'page_not_ready': '聊天标题或输入框未就绪',
    'read_error': '页面读取失败',
    'invalid_title': '标题为空或超出长度限制',
    'invalid_target': '所选名称无效',
    'wrong_page': '当前不是抖音聊天页面',
}


def sanitize_evidence(value):
    if not isinstance(value, dict) or value.get('reason') not in REASONS:
        return None
    clean = {'reason': value['reason']}
    for field in ('expected_name', 'observed_name'):
        name = value.get(field, '')
        if not isinstance(name, str) or len(name) > 256:
            return None
        clean[field] = name
    for field in ('header_count', 'title_count', 'editor_count'):
        number = value.get(field, 0)
        clean[field] = min(max(number, 0), 100) if type(number) is int else 0
    for field in ('header_visible', 'title_visible', 'editor_visible'):
        clean[field] = value.get(field) is True
    return clean


def save_evidence(db, row, diagnostic):
    clean = sanitize_evidence(diagnostic)
    if clean is None:
        return
    evidence = db.get(RecipientCheckEvidence, (row.run_id, row.position))
    if evidence is None:
        evidence = RecipientCheckEvidence(run_id=row.run_id, position=row.position)
        db.add(evidence)
    evidence.diagnostic_json = json.dumps(clean, ensure_ascii=True, sort_keys=True)


def name_approvals(db, account_id, target_uid, target_name):
    rows = db.scalars(select(RecipientNameApproval).where(
        RecipientNameApproval.account_id == account_id,
        RecipientNameApproval.target_sec_uid == (target_uid or ''),
        RecipientNameApproval.target_name == target_name,
        RecipientNameApproval.revoked_at.is_(None),
    )).all()
    return [dict(selected_name=row.selected_name, observed_name=row.observed_name) for row in rows]


def approval_id(row, evidence):
    values = [row.account_id, row.target_sec_uid, row.target_name,
              evidence['expected_name'], evidence['observed_name']]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()


class RecipientReviewService:
    def __init__(self, db):
        self.db = db

    def load(self, actor, run_id, position):
        run = self.db.get(TaskRun, run_id)
        task = self.db.get(SparkTask, run.task_id) if run else None
        row = self.db.get(TaskRunRecipient, (run_id, position))
        if not task or not row or actor.status != 'active' or (
                actor.role != 'admin' and task.owner_user_id != actor.id):
            raise NotFound('执行记录不存在')
        stored = self.db.get(RecipientCheckEvidence, (run_id, position))
        evidence = sanitize_evidence(json.loads(stored.diagnostic_json)) if stored else None
        approval = self.db.get(RecipientNameApproval, approval_id(row, evidence)) if evidence else None
        digest = hashlib.sha256(json.dumps([run_id, position, row.account_id,
            row.target_sec_uid, row.target_name, evidence], sort_keys=True).encode()).hexdigest()
        return run, task, row, evidence, approval, digest

    def approve(self, actor, run_id, position, digest):
        run, task, row, evidence, approval, current_digest = self.load(actor, run_id, position)
        if digest != current_digest:
            raise ValidationError('诊断内容已变化，请刷新后重新检查')
        current = self.db.get(SparkTaskRecipient, (task.id, position))
        account = self.db.get(DouyinAccount, row.account_id)
        if not current or not account or account.owner_user_id != task.owner_user_id or (
            task.douyin_account_id != row.account_id or
            (current.target_sec_uid, current.target_name) != (row.target_sec_uid, row.target_name)
        ):
            raise ValidationError('账号或好友已修改，请查看新执行记录')
        if not evidence or evidence['reason'] not in {'name_mismatch', 'human_approved'} or not (
            evidence['expected_name'] and evidence['observed_name'] and
            all(evidence[key] == 1 for key in ('header_count', 'title_count', 'editor_count')) and
            all(evidence[key] for key in ('header_visible', 'title_visible', 'editor_visible'))
        ):
            raise ValidationError('没有可确认的名称差异；请等待新的诊断，不能跳过页面就绪检查')
        if approval is None:
            approval = RecipientNameApproval(id=approval_id(row, evidence), account_id=row.account_id,
                target_sec_uid=row.target_sec_uid, target_name=row.target_name,
                selected_name=evidence['expected_name'], observed_name=evidence['observed_name'],
                actor_user_id=actor.id, source_run_id=run.id)
            self.db.add(approval)
        approval.revoked_at = None
        approval.actor_user_id = actor.id
        AuditService(self.db).write(actor.id, 'recipient.name_approved', 'task_run', run.id,
                                   detail=f'position={position};approval={approval.id}')
        self.db.flush()

    def revoke(self, actor, run_id, position):
        run, _task, _row, _evidence, approval, _digest = self.load(actor, run_id, position)
        if approval and approval.revoked_at is None:
            approval.revoked_at = datetime.now(timezone.utc)
            AuditService(self.db).write(actor.id, 'recipient.name_revoked', 'task_run', run.id,
                                       detail=f'position={position};approval={approval.id}')
        self.db.flush()


def display_evidence(evidence):
    if not evidence:
        return None
    result = dict(evidence, reason_label=REASONS[evidence['reason']])
    for key in ('expected_name', 'observed_name'):
        result[key + '_escaped'] = json.dumps(evidence[key], ensure_ascii=True)
        result[key + '_codepoints'] = ' '.join(f'U+{ord(char):04X}' for char in evidence[key])
    return result
