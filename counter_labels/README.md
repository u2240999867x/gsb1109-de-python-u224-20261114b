# 离线 CSS 计数器条款标注（counter_labels）

法规页面里的“第 2.3 条”通常写在 `::before` 的 `content` 里，正文 DOM 节点
并不包含这些文字。本包在**没有浏览器、不联网**的前提下，用标准库解析 HTML、
用 `tinycss2` 解析 CSS，重建伪元素最终渲染出来的条款标签，并给出可回链的
证据。

## 三份离线产物

`Labeler(document, stylesheet).run()` 返回 `LabelingResult`，其 `to_dict()`
一次性导出：

1. `node_tree`：离线节点树（`counter_labels.htmltree`），每个元素带稳定
   路径（如 `/html/body/section[1]/article[2]`）、`doc_id`、源码行列号和
   属性；文本节点只保留文档里真实存在的文字。
2. `tinycss2_ast`：`Stylesheet.ast` 即 `tinycss2.parse_stylesheet` 的原始
   解析结果；`matched_declarations` 列出每个节点 / 伪元素实际匹配到的全部
   声明（含选择器、特异度、`!important`、CSS 源码行）。
3. `labels`：每个由 `content` 生成的标签，带渲染文本、回链锚点
   （`节点路径::before|after`）、读取时刻的计数器值和完整证据链。

## 计数器语义（`counter_labels/counters.py`）

- `counter-reset` 压入新的计数器作用域，元素结束时弹出，支持任意嵌套；
  外层计数器不被内层 reset 影响，下一节继续累加。
- `counter-set` 写最内层实例（无实例则创建），`counter-increment` 更新最
  内层实例，缺省步长为 1；裸 `counter-increment` 作用在 reversed 实例上时
  缺省步长为 -1，与 CSS Lists 规范一致。
- `reversed(name N)` 使用给定初值 N；`reversed(name)` 缺初值时，初值等于
  该作用域内实际自增该计数器的有效条目数（第一遍遍历统计）。
- reversed 的递减发生在本元素 `::before` 读取**之后**，因此标记依次显示
  N、N-1、…、1，并在 0 处钳制。
- 伪元素读取时机：`::before` 等价于第一个子元素（在本元素 reset/set/
  increment 之后、任何子元素之前读取），`::after` 等价于最后一个子元素
  （在所有后代操作之后读取），两者各自的 counter 属性也按同一顺序生效。
- 支持 `counter()`、`counters()`（带分隔符的完整作用域链）以及
  decimal / alpha / roman / greek 等计数样式。

## 改一条子款后的差异（`counter_labels/diff.py`）

`diff_labelings(before, after)` 先用与位置无关的身份键（元素 id，否则
tag + 自身文本 + 祖先链）做 LCS 对齐，再分类：

- `unchanged` / `changed` / `added` / `removed`，
- 每个 changed 标签给出原因：`counter-value`（重新编号）、
  `style-evidence`（样式声明变了）、`dom-text`（正文文本变了），
- `style_only=True` 表示新标签文本只来自样式生成、正文节点里不存在，
  即“证据只来自样式”，
- `downstream_of(anchor)` 按文档顺序筛出编辑点之后发生变化的标签。

## 示例

```bash
python examples/regulation_page/build_artifacts.py
```

会打印全部标签并写出 `examples/regulation_page/artifacts.json`，其中
`edit_scenario` 演示删除一个倒序子款后，哪些后续标签被重新编号、哪些证据
仅来自样式。

## 测试

```bash
python -m pytest tests/
```
