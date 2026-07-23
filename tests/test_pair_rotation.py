from utils.pair_rotation import PairRotationStore


def test_pick_next_untried_domain_sequential():
  domains = ["a.com", "b.com", "c.com"]
  tried = {"a.com"}
  assert PairRotationStore.pick_next_untried_domain(domains, tried) == "b.com"
  tried.add("b.com")
  assert PairRotationStore.pick_next_untried_domain(domains, tried) == "c.com"
  tried.add("c.com")
  assert PairRotationStore.pick_next_untried_domain(domains, tried) is None


def test_pick_next_untried_domain_single_site():
  assert PairRotationStore.pick_next_untried_domain(["king.com"], {"king.com"}) is None


def test_pick_next_untried_domain_normalizes_www():
  tried = {"www.foo.com"}
  assert PairRotationStore.pick_next_untried_domain(["foo.com", "bar.com"], tried) == "bar.com"


def test_allocate_pair_skips_session_exhausted_pairs(tmp_path):
  store = PairRotationStore(tmp_path / "pair_rotation.json")
  keywords = ["kw-a", "kw-b"]
  domains = ["site.com"]
  skip = {PairRotationStore.pair_key("kw-a", "site.com")}

  kw, dom = store.allocate_pair(keywords, domains, skip_pairs=skip)
  assert (kw, dom) == ("kw-b", "site.com")

  kw, dom = store.allocate_pair(keywords, domains, skip_pairs=skip)
  assert (kw, dom) == ("kw-b", "site.com")


def test_allocate_pair_returns_empty_when_all_session_skipped(tmp_path):
  store = PairRotationStore(tmp_path / "pair_rotation.json")
  keywords = ["only-kw"]
  domains = ["only.com"]
  skip = {PairRotationStore.pair_key("only-kw", "only.com")}

  assert store.allocate_pair(keywords, domains, skip_pairs=skip) == ("", "")


def test_allocate_pair_skip_normalizes_domain_www(tmp_path):
  store = PairRotationStore(tmp_path / "pair_rotation.json")
  skip = {("kw", "www.foo.com")}

  kw, dom = store.allocate_pair(["kw"], ["foo.com", "bar.com"], skip_pairs=skip)
  assert (kw, dom) == ("kw", "bar.com")
