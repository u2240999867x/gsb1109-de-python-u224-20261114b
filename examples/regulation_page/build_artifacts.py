"""Build the offline artifacts for the regulation page example.

Run with::

    python examples/regulation_page/build_artifacts.py

It writes ``artifacts.json`` next to this file containing:

* the offline node tree (with paths and source positions),
* the raw tinycss2 parse result,
* every declaration matched by each node / pseudo-element,
* the back-linkable clause labels with their counter evidence,
* a before/after diff after editing one sub-clause.
"""

import json
import pathlib

from counter_labels import (
    Labeler, Stylesheet, diff_labelings, parse_html)

HERE = pathlib.Path(__file__).parent


def build():
    html_text = (HERE / 'regulation.html').read_text(encoding='utf-8')
    css_text = (HERE / 'regulation.css').read_text(encoding='utf-8')

    document = parse_html(html_text)
    stylesheet = Stylesheet(css_text)
    result = Labeler(document, stylesheet).run()

    edited_html = html_text.replace(
        '        <li>无初值子款四</li>\n', '')
    edited_document = parse_html(edited_html)
    edited_result = Labeler(edited_document, stylesheet).run()
    changes = diff_labelings(result, edited_result)

    artifacts = {
        'node_tree': result.to_dict()['node_tree'],
        'tinycss2_parse': {
            'top_level_node_types': [node.type for node in stylesheet.ast],
            'serialized_rules': [node.serialize() for node in stylesheet.ast],
            'parse_errors': [
                {'kind': error.kind, 'message': error.message,
                 'line': error.source_line}
                for error in stylesheet.errors],
        },
        'matched_declarations': result.to_dict()['matched_declarations'],
        'counter_read_points': result.read_points,
        'labels': [label.to_dict() for label in result.labels],
        'edit_scenario': {
            'description': '删除 reversed-default 清单中的一个子款',
            'diff': changes.to_dict(),
            'style_evidence_only_anchors': [
                change.anchor for change in changes.style_evidence_only()],
        },
    }
    output = HERE / 'artifacts.json'
    output.write_text(
        json.dumps(artifacts, ensure_ascii=False, indent=2),
        encoding='utf-8')
    return artifacts


if __name__ == '__main__':
    bundle = build()
    for label in bundle['labels']:
        print('%s -> %s' % (label['anchor'], label['text']))
    print('artifacts written to examples/regulation_page/artifacts.json')
