"""أدوار الشركة وصلاحياتها — مصدر وحيد للحقيقة."""

ROLE_OWNER = "owner"
ROLE_MANAGER = "manager"
ROLE_ACCOUNTANT = "accountant"
ROLE_SALES = "sales"
ROLE_STAFF = "staff"
ROLE_VIEWER = "viewer"

ROLE_LABELS = {
    ROLE_OWNER: "مالك",
    ROLE_MANAGER: "مدير",
    ROLE_ACCOUNTANT: "محاسب",
    ROLE_SALES: "مبيعات",
    ROLE_STAFF: "موظف",
    ROLE_VIEWER: "مشاهد",
}

# صلاحيات ذرّية
P_RECORD_CREATE = "record.create"
P_RECORD_EDIT_OWN = "record.edit_own"
P_RECORD_EDIT_ANY = "record.edit_any"
P_RECORD_DELETE = "record.delete"
P_REPORT_ALL = "report.view_all"
P_REPORT_OWN = "report.view_own"
P_EXPORT = "export"
P_BUDGET_MANAGE = "budget.manage"
P_MEMBERS_MANAGE = "members.manage"
P_COMPANY_MANAGE = "company.manage"

_ALL = {
    P_RECORD_CREATE,
    P_RECORD_EDIT_OWN,
    P_RECORD_EDIT_ANY,
    P_RECORD_DELETE,
    P_REPORT_ALL,
    P_REPORT_OWN,
    P_EXPORT,
    P_BUDGET_MANAGE,
    P_MEMBERS_MANAGE,
    P_COMPANY_MANAGE,
}

ROLE_PERMISSIONS = {
    ROLE_OWNER: set(_ALL),
    ROLE_MANAGER: _ALL - {P_COMPANY_MANAGE},
    ROLE_ACCOUNTANT: {P_RECORD_CREATE, P_RECORD_EDIT_OWN, P_REPORT_ALL, P_REPORT_OWN, P_EXPORT, P_BUDGET_MANAGE},
    ROLE_SALES: {P_RECORD_CREATE, P_RECORD_EDIT_OWN, P_REPORT_OWN},
    ROLE_STAFF: {P_RECORD_CREATE, P_RECORD_EDIT_OWN},
    ROLE_VIEWER: {P_REPORT_ALL, P_REPORT_OWN},
}

# أدوار يجوز للمدير منحها (المدير لا يمنح owner/manager)
ASSIGNABLE_BY_MANAGER = {ROLE_ACCOUNTANT, ROLE_SALES, ROLE_STAFF, ROLE_VIEWER}

BUSINESS_TYPES = {
    "trade": "تجارة",
    "services": "خدمات",
    "restaurant": "مطعم/كافيه",
    "contracting": "مقاولات",
    "manufacturing": "تصنيع",
    "other": "أخرى",
}


def role_has(role: str | None, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role or "", set())
