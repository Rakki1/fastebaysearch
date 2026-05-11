from fastebaysearch_app.models import SearchResult
from fastebaysearch_app.notifications.html_report import HtmlReportNotifier


def result(item_id="1", name="Camera"):
    return SearchResult(
        keywords="camera",
        name=name,
        ebay_site="EBAY_US",
        price="10.00 EUR",
        item_id=item_id,
        link="https://www.ebay.com/itm/1",
        seller="seller",
        starts="Not available",
        ends="Not available",
    )


def test_html_report_writes_email_html_to_report_directory(tmp_path):
    notifier = HtmlReportNotifier(tmp_path)

    sent = notifier.send([result()], "Tester")

    reports = list(tmp_path.glob("ebay_report_*.html"))
    assert sent == [result()]
    assert len(reports) == 1
    content = reports[0].read_text(encoding="utf-8")
    assert "Hello Tester" in content
    assert "Camera" in content
    assert "https://www.ebay.com/itm/1" in content


def test_html_report_uses_unique_timestamped_filenames(tmp_path):
    notifier = HtmlReportNotifier(tmp_path)

    notifier.send([result()], "User")
    notifier.send([result("2", "Lens")], "User")

    reports = list(tmp_path.glob("ebay_report_*.html"))
    assert len(reports) == 2
    assert reports[0].name != reports[1].name


def test_html_report_does_not_create_file_for_empty_results(tmp_path):
    notifier = HtmlReportNotifier(tmp_path)

    sent = notifier.send([], "User")

    assert sent == []
    assert list(tmp_path.glob("*.html")) == []


def test_html_report_respects_max_per_run(tmp_path):
    notifier = HtmlReportNotifier(tmp_path, max_per_run=1)

    sent = notifier.send([result("1", "Camera"), result("2", "Lens")], "User")

    reports = list(tmp_path.glob("ebay_report_*.html"))
    content = reports[0].read_text(encoding="utf-8")
    assert [item.item_id for item in sent] == ["1"]
    assert "Camera" in content
    assert "Lens" not in content
