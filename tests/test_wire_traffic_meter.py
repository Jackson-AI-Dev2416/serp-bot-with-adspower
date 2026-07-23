from utils.wire_traffic_meter import WireTrafficMeter


def test_wire_meter_counts_other_until_site_visit_marked():
  meter = WireTrafficMeter()
  meter._record_wire(1000)
  meter._record_wire(500)
  assert meter.wire_other_bytes == 1500
  assert meter.wire_target_bytes == 0


def test_wire_meter_counts_site_after_mark():
  meter = WireTrafficMeter()
  meter._record_wire(200)
  meter.mark_site_visit_started()
  meter._record_wire(3000)
  meter._record_wire(700)
  assert meter.wire_other_bytes == 200
  assert meter.wire_target_bytes == 3700


def test_wire_meter_pending_delta_respects_phase():
  meter = WireTrafficMeter()
  meter._record_wire(100)
  delta, target, other = meter.take_pending_delta(force=True)
  assert delta == 100
  assert target == 0
  assert other == 100

  meter.mark_site_visit_started()
  meter._record_wire(250)
  delta, target, other = meter.take_pending_delta(force=True)
  assert delta == 250
  assert target == 250
  assert other == 0
