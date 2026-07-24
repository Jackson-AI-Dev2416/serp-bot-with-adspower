from ui.main_window import UiMainWindow


def test_format_overall_clicks_kpi():
  assert UiMainWindow._format_overall_clicks_kpi(0, 0) == "0 / 0 / 0%"
  assert UiMainWindow._format_overall_clicks_kpi(13, 6) == "13 / 6 / 68%"
  assert UiMainWindow._format_overall_clicks_kpi(10, 0) == "10 / 0 / 100%"


def test_format_bytes_html_smaller_units():
  plain = UiMainWindow._format_bytes(1536)
  html = UiMainWindow._format_bytes_html(1536, number_pixel_size=26)
  assert plain == "1.50 KB"
  assert "font-size:26px" in html
  assert "1.50" in html
  assert "font-size:13px" in html
  assert "KB" in html
  assert html.index("font-size:26px") < html.index("font-size:13px")


def test_traffic_unit_font_px_half_of_number_size():
  assert UiMainWindow._traffic_unit_font_px(26) == 13
  assert UiMainWindow._traffic_unit_font_px(24) == 12
