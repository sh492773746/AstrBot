"""Keep deployed read-only acceptance aligned with the two-level page inventory."""

from pathlib import Path

from scripts import check_wsl_readonly_retest as readonly


def test_readonly_page_inventory_covers_all_integrated_tabs_once():
    source = (
        Path(__file__).resolve().parents[1]
        / "dashboard/plugin-pages/wangshangliao/App.vue"
    ).read_text()
    assert len(readonly.PAGE_SECTIONS) == 5
    views = [view for tabs in readonly.PAGE_SECTIONS.values() for view in tabs]
    assert len(views) == len(set(views)) == 19
    for section in readonly.PAGE_SECTIONS:
        assert f'{{ id: "{section}", name:' in source
    for view in views:
        assert f'id: "{view}"' in source
    assert not {"settings", "business"} & set(views)


def test_readonly_api_allowlist_never_includes_writes_or_test_probes():
    assert readonly.READ_ACTIONS == {
        "status",
        "finance_status",
        "finance_members",
        "tenant_members",
    }
    assert "test_probe" not in readonly.READ_ACTIONS
    assert not {"finance_grant", "tenant", "billing_settings"} & readonly.READ_ACTIONS
