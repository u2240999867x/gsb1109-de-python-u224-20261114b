"""Tests for the offline HTML tree."""

from counter_labels.htmltree import TextNode, parse_html


def test_tree_structure_and_paths():
    document = parse_html(
        '<html><body><section id="s1" class="chapter">'
        '<p>alpha</p><p>beta</p></section>'
        '<section id="s2"><p>gamma</p></section>'
        '</body></html>')
    root = document.root
    assert root.tag == 'html'
    body = root.children[0]
    sections = [child for child in body.children if child.tag == 'section']
    assert [section.id for section in sections] == ['s1', 's2']
    paragraphs = sections[0].children
    assert paragraphs[0].path == '/html/body/section[1]/p[1]'
    assert paragraphs[1].path == '/html/body/section[1]/p[2]'
    assert sections[0].path == '/html/body/section[1]'
    assert sections[1].path == '/html/body/section[2]'
    assert document.find_id('s2').path == '/html/body/section[2]'
    assert document.find_path(
        '/html/body/section[1]/p[2]') is paragraphs[1]


def test_document_order_ids_are_stable():
    document = parse_html('<div><span>a</span><span>b</span></div>')
    ids = [node.doc_id for node in document.root.iter(include_text=True)]
    assert ids == sorted(ids)
    assert document.node_count == len(ids)


def test_text_nodes_hold_only_source_text():
    document = parse_html(
        '<article><p>第 一 条</p></article>')
    paragraph = document.find_path('/html/body/article/p')
    text_node = next(child for child in paragraph.children
                     if isinstance(child, TextNode))
    assert text_node.data == '第 一 条'
    assert paragraph.text_content() == '第 一 条'


def test_source_positions_and_attributes():
    document = parse_html(
        '<html>\n<body>\n  <ol class="sub" data-kind="rev">\n'
        '    <li>x</li>\n  </ol>\n</body>\n</html>')
    ol = document.find_path('/html/body/ol')
    assert ol.source_line == 3
    assert ol.get_attribute('data-kind') == 'rev'
    assert ol.classes == ['sub']


def test_void_elements_do_not_swallow_siblings():
    document = parse_html(
        '<html><body><hr><p>after</p><img src="x.png"></body></html>')
    body = document.root.children[0]
    tags = [child.tag for child in body.children]
    assert tags == ['hr', 'p', 'img']
