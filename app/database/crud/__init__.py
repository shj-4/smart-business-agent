"""
واجهة الوصول الموحّدة لدوال CRUD — تُقسَّم التنفيذات إلى وحدات فرعية
لكن هذا الملف يعيد تصدير كل الأسماء بحيث يبقى `from app.database.crud import X`
مستقرًا لكل المستوردين. الاعتماديات بين الوحدات تُستورد حيًا داخل الدوال،
والمُجمِّعات النقدية المشتركة تُدار في app.money، وعزل crud عن الإدارة عبر app.events.

اكتمال إعادة التصدير (كل اسم من كل وحدة) مراقَب آليًا في
tests/test_crud_split.py::test_facade_exposes_all_names_from_their_module —
لا تُضاف قائمة __all__ هنا: الاسم يُكتَب في موضع واحد فقط (سطر الاستيراد)
حتى لا تتدفق التعديلات عند إعادة التسمية.
"""

from app.money import (
    _best_effort_base_amount,
    _split_stored_base,
    _sum_amounts_by_currency,
    _unified_totals_for_rows,
)

from .budgets import (
    _current_month_key,
    budget_monthly_reset,
    budget_usage,
    create_budget,
    delete_budget,
    get_budget,
    list_budgets,
    person_debts,
)
from .common import (
    CURRENCY_ALIASES,
    MAX_DESCRIPTION_LEN,
    _clean_person,
    _clean_text,
    _invalidate_caches,
    _is_duplicate_message,
    _like_escape,
    _to_decimal,
    normalize_currency,
    parse_date_local,
)
from .credit import (
    credit_monthly_reset,
    credit_usage,
    get_credit_limit,
    list_credit_limits,
    set_credit_limit,
)
from .feedback import (
    list_correction_feedback,
    mark_correction_reviewed,
    record_correction_feedback,
)
from .invoices import (
    ORDER_STATUSES,
    create_invoice,
    list_invoices,
    list_orders,
    mark_invoice_paid,
    mark_overdue_invoices,
    set_order_status,
)
from .prefs import (
    get_user_lang,
    set_user_lang,
)
from .records import (
    _run_query_uncached,
    _search_row_result,
    create_note,
    create_transaction,
    delete_record_by_id,
    get_comparison_ranges,
    get_period_range,
    get_record_by_id,
    list_recent_records,
    restore_last_deleted,
    run_query,
    search_records,
    soft_delete_last,
    undo_last_record,
    update_note,
    update_transaction,
)
from .reports import (
    _linear_forecast,
    _unified_amount_safe,
    db_size_bytes,
    deviation_summary,
    forecast_totals,
    get_report_pref,
    list_report_prefs,
    mark_report_sent,
    monthly_totals,
    set_report_frequency,
    user_ids_with_data,
    user_stats,
)
from .tasks import (
    _MAX_DT,
    _PRIORITY_RANK,
    _TASK_FETCH_CAP,
    _normalize_priority,
    _normalize_recurrence,
    _person_filter,
    _priority_sort,
    _respawn_recurring_task,
    _task_order_by,
    complete_task,
    create_task,
    delete_task_by_id,
    find_pending_task,
    list_done_tasks,
    list_overdue_tasks,
    list_pending_tasks,
    mark_overdue_tasks,
    update_task,
)
from .workspace import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    accept_workspace_invite,
    accessible_user_ids,
    can_manage_records,
    create_workspace,
    decline_workspace_invite,
    dissolve_workspace,
    invite_to_workspace,
    is_workspace_owner,
    leave_workspace,
    list_workspace,
    pending_workspace_invite,
    remove_from_workspace,
    transfer_workspace_ownership,
    workspace_for_user,
    workspace_member_ids,
)
