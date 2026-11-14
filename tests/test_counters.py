"""Tests for scopes, reset/set/increment, reversed counters and read timing."""

from counter_labels.counters import CounterEngine, format_counter
from counter_labels.cssmodel import Stylesheet
from counter_labels.htmltree import parse_html
from counter_labels.labeling import Labeler


def run(html, css):
    document = parse_html(html)
    stylesheet = Stylesheet(css)
    return document, stylesheet, Labeler(document, stylesheet).run()


def label_texts(result):
    return {label.anchor: label.text for label in result.labels}


BASIC_HTML = """
<html><body>
  <section class="chapter" id="c1">
    <article class="clause" id="a1"><p>1</p></article>
    <article class="clause" id="a2"><p>2</p></article>
  </section>
  <section class="chapter" id="c2">
    <article class="clause" id="a3"><p>3</p></article>
  </section>
</body></html>
"""

BASIC_CSS = """
body { counter-reset: chapter; }
.chapter { counter-increment: chapter; counter-reset: clause; }
.chapter .clause { counter-increment: clause; }
.clause::before { content: counter(chapter) "." counter(clause); }
"""


def test_outer_counter_continues_inner_resets_each_section():
    _, _, result = run(BASIC_HTML, BASIC_CSS)
    texts = label_texts(result)
    assert texts['/html/body/section[1]/article[1]::before'] == '1.1'
    assert texts['/html/body/section[1]/article[2]::before'] == '1.2'
    assert texts['/html/body/section[2]/article::before'] == '2.1'


def test_counter_set_overrides_value():
    css = BASIC_CSS + """
    #a2 { counter-set: clause 9; counter-increment: clause 0; }
    """
    _, _, result = run(BASIC_HTML, css)
    texts = label_texts(result)
    assert texts['/html/body/section[1]/article[1]::before'] == '1.1'
    assert texts['/html/body/section[1]/article[2]::before'] == '1.9'
    assert texts['/html/body/section[2]/article::before'] == '2.1'


def test_before_and_after_read_at_different_times():
    html = """
    <html><body>
      <ol><li id="x1">a</li><li id="x2">b</li></ol>
    </body></html>
    """
    css = """
    ol { counter-reset: item; }
    li { counter-increment: item; }
    li::before { content: "before=" counter(item); }
    li::after { content: "after=" counter(item); }
    """
    _, _, result = run(html, css)
    texts = label_texts(result)
    first = '/html/body/ol/li[1]'
    second = '/html/body/ol/li[2]'
    assert texts[first + '::before'] == 'before=1'
    assert texts[first + '::after'] == 'after=1'
    assert texts[second + '::before'] == 'before=2'
    assert texts[second + '::after'] == 'after=2'


def test_pseudo_increment_applies_before_its_own_content_read():
    html = '<html><body><p id="p">x</p></body></html>'
    css = """
    body { counter-reset: n; }
    p::before { counter-increment: n; content: counter(n); }
    p::after { content: counter(n); }
    """
    _, _, result = run(html, css)
    texts = label_texts(result)
    assert texts['/html/body/p::before'] == '1'
    assert texts['/html/body/p::after'] == '1'


def test_after_pseudo_reads_after_children():
    html = """
    <html><body>
      <section id="s">
        <p class="step">a</p><p class="step">b</p>
      </section>
    </body></html>
    """
    css = """
    section { counter-reset: step; }
    .step { counter-increment: step; }
    section::after { content: "total=" counter(step); }
    """
    _, _, result = run(html, css)
    assert label_texts(result)['/html/body/section::after'] == 'total=2'


def test_reversed_counter_with_explicit_start():
    html = """
    <html><body><ol class="r">
      <li>a</li><li>b</li><li>c</li>
    </ol></body></html>
    """
    css = """
    ol.r { counter-reset: reversed(item 3); }
    ol.r > li { counter-increment: item -1; }
    li::before { content: counter(item); }
    """
    _, _, result = run(html, css)
    texts = list(label_texts(result).values())
    assert texts == ['3', '2', '1']


def test_reversed_counter_default_start_is_entry_count():
    html = """
    <html><body><ol class="r">
      <li>a</li><li>b</li><li>c</li><li>d</li>
    </ol></body></html>
    """
    css = """
    ol.r { counter-reset: reversed(item); }
    ol.r > li { counter-increment: item -1; }
    li::before { content: counter(item); }
    """
    _, _, result = run(html, css)
    texts = list(label_texts(result).values())
    assert texts == ['4', '3', '2', '1']


def test_reversed_counter_scope_does_not_leak_to_next_list():
    html = """
    <html><body>
      <ol class="r" id="l1"><li>a</li><li>b</li></ol>
      <ol class="r" id="l2"><li>c</li><li>d</li><li>e</li></ol>
    </body></html>
    """
    css = """
    ol.r { counter-reset: reversed(item); }
    ol.r > li { counter-increment: item -1; }
    li::before { content: counter(item); }
    """
    _, _, result = run(html, css)
    texts = label_texts(result)
    first = [texts['/html/body/ol[1]/li[%d]::before' % i]
             for i in (1, 2)]
    second = [texts['/html/body/ol[2]/li[%d]::before' % i]
              for i in (1, 2, 3)]
    assert first == ['2', '1']
    assert second == ['3', '2', '1']


def test_counters_function_reads_full_scope_chain():
    html = """
    <html><body>
      <section class="chapter"><article class="clause">
        <ol><li>x</li></ol>
      </article></section>
    </body></html>
    """
    css = """
    body { counter-reset: chapter; }
    .chapter { counter-increment: chapter; counter-reset: clause; }
    .clause { counter-increment: clause; }
    li { counter-reset: chapter; }
    li::before { content: counters(chapter, "."); }
    """
    _, _, result = run(html, css)
    assert label_texts(result)[
        '/html/body/section/article/ol/li::before'] == '1.0'


def test_implicit_creation_by_increment_starts_at_zero():
    html = '<html><body><p id="p">x</p></body></html>'
    css = """
    p { counter-increment: ghost; }
    p::before { content: counter(ghost); }
    """
    _, _, result = run(html, css)
    assert label_texts(result)['/html/body/p::before'] == '1'


def test_engine_exposes_counter_scopes_at_read_points():
    document = parse_html(BASIC_HTML)
    stylesheet = Stylesheet(BASIC_CSS)
    engine = CounterEngine(document, stylesheet)
    points = []
    engine.run(lambda element, pseudo, state: points.append(
        (element.id, pseudo,
         {name: [i.value for i in stack]
          for name, stack in state.items()})))
    a1_before = next(state for node_id, pseudo, state in points
                     if node_id == 'a1' and pseudo == 'before')
    assert a1_before == {'chapter': [1], 'clause': [1]}
    a3_before = next(state for node_id, pseudo, state in points
                     if node_id == 'a3' and pseudo == 'before')
    assert a3_before == {'chapter': [2], 'clause': [1]}


def test_counter_styles():
    assert format_counter(3, 'decimal') == '3'
    assert format_counter(3, 'lower-alpha') == 'c'
    assert format_counter(27, 'upper-alpha') == 'AA'
    assert format_counter(4, 'lower-roman') == 'iv'
    assert format_counter(14, 'upper-roman') == 'XIV'
    assert format_counter(5, 'lower-greek') == 'ε'
    assert format_counter(7, 'decimal-leading-zero') == '07'
