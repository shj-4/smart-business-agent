"""
Unit tests لـ app.schemas.normalize_analysis — تصحيح JSON مشوّه تلقائيًا.

الهدف: التأكد أن أي إخراج خاطئ من الـ AI (حقول ناقصة، أنواع خاطئة، بنى
غريبة) لا يُسقط المعالجة بل يُصحَّح إلى dict موحّد آمن.
"""

from app.schemas import normalize_analysis


class TestNormalizeAnalysis:
    def test_valid_structure_preserved(self):
        data = {
            "intent": "record",
            "type": "expense",
            "amount": 300.0,
            "currency": "ILS",
            "person": "محمد",
            "description": "شراء مواد",
            "date": "2026-09-03 10:00",
            "missing_fields": [],
            "query_details": None,
        }
        result = normalize_analysis(data)
        assert result["intent"] == "record"
        assert result["type"] == "expense"
        assert result["amount"] == 300.0
        assert result["currency"] == "ILS"
        assert result["person"] == "محمد"
        assert result["missing_fields"] == []
        assert result["query_details"] is None

    def test_missing_fields_become_defaults(self):
        result = normalize_analysis({"intent": "record"})
        assert result["intent"] == "record"
        assert result["type"] is None
        assert result["amount"] is None
        assert result["currency"] is None
        assert result["person"] is None
        assert result["description"] is None
        assert result["date"] is None
        assert result["missing_fields"] == []
        assert result["query_details"] is None

    def test_amount_as_string_with_text(self):
        result = normalize_analysis({"amount": "دفعت 300 شيكل"})
        assert result["amount"] == 300.0

    def test_amount_as_comma_decimal_string(self):
        result = normalize_analysis({"amount": "12,5"})
        assert result["amount"] == 12.5

    def test_amount_as_wrong_type_becomes_none(self):
        assert normalize_analysis({"amount": ["300"]})["amount"] is None
        assert normalize_analysis({"amount": {"value": 300}})["amount"] is None
        assert normalize_analysis({"amount": True})["amount"] is None

    def test_text_fields_with_complex_types(self):
        result = normalize_analysis({"person": {"name": "محمد"}, "description": [1, 2]})
        assert result["person"] is None
        assert result["description"] is None

    def test_text_fields_stripped(self):
        result = normalize_analysis({"description": "  مهمة   "})
        assert result["description"] == "مهمة"

    def test_query_details_parsed(self):
        data = {
            "intent": "query",
            "query_details": {"metric": "total_expenses", "period": "this_month", "person": "محمد"},
        }
        result = normalize_analysis(data)
        qd = result["query_details"]
        assert qd["metric"] == "total_expenses"
        assert qd["period"] == "this_month"
        assert qd["person"] == "محمد"

    def test_query_details_wrong_type_ignored(self):
        result = normalize_analysis({"intent": "query", "query_details": "garbage"})
        assert result["query_details"] is None

    def test_missing_fields_as_string_and_dict(self):
        as_str = normalize_analysis({"missing_fields": "amount"})
        assert as_str["missing_fields"] == ["amount"]

        as_dict = normalize_analysis({"missing_fields": {"amount": True, "date": True}})
        assert sorted(as_dict["missing_fields"]) == ["amount", "date"]

    def test_extra_keys_ignored(self):
        result = normalize_analysis({"intent": "chat", "unexpected": 1, "ghost": "x"})
        assert "unexpected" not in result
        assert "ghost" not in result
        assert result["intent"] == "chat"

    def test_non_dict_input_safe_result(self):
        result = normalize_analysis("نص وليس JSON")
        assert result["intent"] == "unknown"
        assert result["error"] == "invalid_structure"

        result_list = normalize_analysis([1, 2, 3])
        assert result_list["intent"] == "unknown"
        assert result_list["error"] == "invalid_structure"

    def test_output_is_json_serializable_dict(self):
        import json

        result = normalize_analysis(
            {"intent": "record", "amount": "٣٠٠", "missing_fields": "person"}
        )
        # يجب ألا يرمي — كل القيم قابلة للـ JSON
        dumped = json.dumps(result, ensure_ascii=False)
        assert isinstance(dumped, str)


class TestPriorityRecurrenceNormalization:
    def test_priority_high_arabic(self):
        result = normalize_analysis({"priority": "عاجل"})
        assert result["priority"] == "high"

    def test_priority_low_and_normal(self):
        assert normalize_analysis({"priority": "منخفضة"})["priority"] == "low"
        assert normalize_analysis({"priority": "عادية"})["priority"] == "normal"
        assert normalize_analysis({"priority": None})["priority"] is None

    def test_recurrence_arabic(self):
        assert normalize_analysis({"recurrence": "كل أسبوع"})["recurrence"] == "weekly"
        assert normalize_analysis({"recurrence": "شهري"})["recurrence"] == "monthly"
        assert normalize_analysis({"recurrence": "كل يوم"})["recurrence"] == "daily"
        assert normalize_analysis({"recurrence": "غريب"})["recurrence"] is None

    def test_priority_recurrence_absent_defaults(self):
        result = normalize_analysis({"intent": "record", "type": "task"})
        assert result["priority"] is None
        assert result["recurrence"] is None
