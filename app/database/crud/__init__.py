"""
واجهة الوصول الموحّدة لدوال CRUD — تُقسَّم التنفيذات إلى وحدات فرعية
لكن هذا الملف يعيد تصدير كل الأسماء بحيث يبقى `from app.database.crud import X`
مستقرًا لكل المستوردين. الاعتماديات بين الوحدات تُستورد حيًا داخل الدوال.
"""

from .common import (
    CURRENCY_ALIASES,
    MAX_DESCRIPTION_LEN,
    normalize_currency,
    _clean_text,
    _clean_person,
    _to_decimal,
    _is_duplicate_message,
    _like_escape,
    parse_date_local,
    _invalidate_caches,
)

from .workspace import (
    workspace_for_user,
    workspace_member_ids,
    accessible_user_ids,
    is_workspace_owner,
    create_workspace,
    invite_to_workspace,
    remove_from_workspace,
    leave_workspace,
    list_workspace,
    transfer_workspace_ownership,
    dissolve_workspace,
    can_manage_records,
)

from .tasks import (
    _PRIORITY_RANK,
    _MAX_DT,
    _TASK_FETCH_CAP,
    _normalize_priority,
    _normalize_recurrence,
    create_task,
    _person_filter,
    _priority_sort,
    _task_order_by,
    list_pending_tasks,
    list_overdue_tasks,
    mark_overdue_tasks,
    list_done_tasks,
    delete_task_by_id,
    find_pending_task,
    complete_task,
    _respawn_recurring_task,
    update_task,
)

from .records import (
    _best_effort_base_amount,
    create_transaction,
    create_note,
    soft_delete_last,
    get_period_range,
    get_comparison_ranges,
    _sum_amounts_by_currency,
    _split_stored_base,
    _unified_totals_for_rows,
    run_query,
    _run_query_uncached,
    undo_last_record,
    restore_last_deleted,
    list_recent_records,
    get_record_by_id,
    delete_record_by_id,
    _search_row_result,
    search_records,
    update_transaction,
    update_note,
)

from .budgets import (
    _current_month_key,
    create_budget,
    list_budgets,
    get_budget,
    delete_budget,
    budget_usage,
    budget_monthly_reset,
    person_debts,
)

from .invoices import (
    ORDER_STATUSES,
    create_invoice,
    list_invoices,
    mark_invoice_paid,
    mark_overdue_invoices,
    list_orders,
    set_order_status,
)

from .credit import (
    set_credit_limit,
    list_credit_limits,
    get_credit_limit,
    credit_usage,
)

from .reports import (
    get_report_pref,
    set_report_frequency,
    list_report_prefs,
    mark_report_sent,
    monthly_totals,
    user_ids_with_data,
    db_size_bytes,
    user_stats,
    _linear_forecast,
    forecast_totals,
    _unified_amount_safe,
    deviation_summary,
)

from .prefs import (
    get_user_lang,
    set_user_lang,
)

from .feedback import (
    record_correction_feedback,
    list_correction_feedback,
    mark_correction_reviewed,
)
