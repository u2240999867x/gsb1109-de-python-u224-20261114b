# HPACK 双策略离线重放差异账

- 策略 A：`tests/test_fixtures/nghttp2`（nghttp2，动态表上限固定 4096，全程不发表大小更新）
- 策略 B：`tests/test_fixtures/nghttp2-change-table-size`（同一 nghttp2 编码器、同一初始表，按 case 发送动态表大小更新并触发驱逐）
- 输入序列：`tests/test_fixtures/raw-data` 中 31 个 story（00–30，31 在 B 侧缺失），两侧字段序列逐字段相同
- 初始状态：RFC 7541 静态表 + 空动态表，maxsize=4096

## 1. 一致性核对

- 重放块数：3267（A/B 各 3267）
- 字节守恒：每个块解析表示的字节之和等于 wire 长度，违例 0 处
- 有序字段：两侧解回字段与 raw-data 声明完全一致，违例 0 处
- 敏感声明（never-indexed）：A/B 两侧字段级声明不一致 0 处
- 与库内 `hpack.Decoder` 交叉解码：0 处不一致
- 勾稽关系：字段级实际成本差额 + 表大小更新差额 = wire 总差额，违例 0 处

## 2. 字节总账（31 个 story 累计）

| 类别 | A 字节 | B 字节 | B−A |
|---|---:|---:|---:|
| **wire 总量** | 348360 | 387941 | 39581 |
| 表大小更新 | 0 | 186 | 186 |
| 索引表示（整字段引用） | 23440 | 21410 | -2030 |
| 字面量表示开销（前缀/长度/名索引） | 34985 | 39630 | 4645 |
| Huffman 字符串（字段名） | 7205 | 10840 | 3635 |
| Huffman 字符串（字段值） | 279847 | 312662 | 32815 |
| 明文字符串（字段名） | 226 | 444 | 218 |
| 明文字符串（字段值） | 2657 | 2769 | 112 |

### 表示方式出现次数

| 表示 | A 次数 | B 次数 |
|---|---:|---:|
| 表大小更新 | 0 | 62 |
| 索引字段 | 23416 | 21392 |
| 字面量（增量索引） | 10823 | 12847 |
| 字面量（不索引） | 3798 | 3798 |
| 字面量（never-indexed） | 0 | 0 |

## 3. 同字段表示迁移矩阵（A → B）

| 迁移 | 次数字段数 | A 字节 | B 字节 | B−A |
|---|---:|---:|---:|---:|
| `indexed -> indexed` | 20893 | 20915 | 20911 | -4 |
| `indexed -> literal-incremental` | 2523 | 2525 | 49728 | 47203 |
| `literal-incremental -> indexed` | 499 | 9058 | 499 | -8559 |
| `literal-incremental -> literal-incremental` | 10324 | 236709 | 237464 | 755 |
| `literal-no-index -> literal-no-index` | 3798 | 79153 | 79153 | 0 |

- 字段级实际成本差额合计（不含表大小更新）：B−A = **39395** 字节
- 表大小更新线上成本：B−A = **186** 字节
- 两项勾稽：39395 + 186 = **39581**，应等于 wire 总差额 39581

## 4. 分 story 差额

| story | 块数 | A 字节 | B 字节 | B−A |
|---|---:|---:|---:|---:|
| story_00.json | 3 | 70 | 76 | 6 |
| story_01.json | 2 | 58 | 64 | 6 |
| story_02.json | 10 | 723 | 729 | 6 |
| story_03.json | 10 | 508 | 514 | 6 |
| story_04.json | 10 | 508 | 514 | 6 |
| story_05.json | 10 | 566 | 572 | 6 |
| story_06.json | 10 | 838 | 844 | 6 |
| story_07.json | 10 | 621 | 627 | 6 |
| story_08.json | 10 | 972 | 978 | 6 |
| story_09.json | 10 | 677 | 683 | 6 |
| story_10.json | 10 | 549 | 555 | 6 |
| story_11.json | 10 | 793 | 799 | 6 |
| story_12.json | 10 | 746 | 752 | 6 |
| story_13.json | 10 | 552 | 558 | 6 |
| story_14.json | 10 | 599 | 605 | 6 |
| story_15.json | 10 | 485 | 491 | 6 |
| story_16.json | 10 | 963 | 969 | 6 |
| story_17.json | 10 | 622 | 628 | 6 |
| story_18.json | 10 | 690 | 696 | 6 |
| story_19.json | 10 | 684 | 690 | 6 |
| story_20.json | 164 | 8729 | 10141 | 1412 |
| story_21.json | 366 | 55470 | 59829 | 4359 |
| story_22.json | 455 | 30766 | 33044 | 2278 |
| story_23.json | 363 | 40015 | 43846 | 3831 |
| story_24.json | 33 | 2769 | 3091 | 322 |
| story_25.json | 256 | 24606 | 26607 | 2001 |
| story_26.json | 117 | 11938 | 12926 | 988 |
| story_27.json | 219 | 40296 | 50096 | 9800 |
| story_28.json | 128 | 14317 | 15798 | 1481 |
| story_29.json | 335 | 40494 | 45896 | 5402 |
| story_30.json | 646 | 66736 | 74323 | 7587 |

## 5. 表大小更新与驱逐明细

### 驱逐总量（后续引用变成长字面量的根因）

| 策略 | 表大小更新触发的驱逐 | 插入新条目触发的驱逐 |
|---|---:|---:|
| A | 0 | 9951 |
| B | 396 | 11789 |

### 策略 A（0 次更新，线上字节 0）


### 策略 B（62 次更新，线上字节 186）

| story | 新上限 | 线上字节 | 本次驱逐条目数 |
|---|---:|---:|---:|
| story_00.json | 1365 | 3 | 0 |
| story_00.json | 2730 | 3 | 0 |
| story_01.json | 1365 | 3 | 0 |
| story_01.json | 2730 | 3 | 0 |
| story_02.json | 1365 | 3 | 0 |
| story_02.json | 2730 | 3 | 0 |
| story_03.json | 1365 | 3 | 0 |
| story_03.json | 2730 | 3 | 0 |
| story_04.json | 1365 | 3 | 0 |
| story_04.json | 2730 | 3 | 0 |
| story_05.json | 1365 | 3 | 0 |
| story_05.json | 2730 | 3 | 0 |
| story_06.json | 1365 | 3 | 0 |
| story_06.json | 2730 | 3 | 0 |
| story_07.json | 1365 | 3 | 0 |
| story_07.json | 2730 | 3 | 0 |
| story_08.json | 1365 | 3 | 0 |
| story_08.json | 2730 | 3 | 0 |
| story_09.json | 1365 | 3 | 0 |
| story_09.json | 2730 | 3 | 0 |
| story_10.json | 1365 | 3 | 0 |
| story_10.json | 2730 | 3 | 0 |
| story_11.json | 1365 | 3 | 0 |
| story_11.json | 2730 | 3 | 0 |
| story_12.json | 1365 | 3 | 0 |
| story_12.json | 2730 | 3 | 0 |
| story_13.json | 1365 | 3 | 0 |
| story_13.json | 2730 | 3 | 0 |
| story_14.json | 1365 | 3 | 0 |
| story_14.json | 2730 | 3 | 0 |
| story_15.json | 1365 | 3 | 0 |
| story_15.json | 2730 | 3 | 0 |
| story_16.json | 1365 | 3 | 0 |
| story_16.json | 2730 | 3 | 0 |
| story_17.json | 1365 | 3 | 0 |
| story_17.json | 2730 | 3 | 0 |
| story_18.json | 1365 | 3 | 0 |
| story_18.json | 2730 | 3 | 0 |
| story_19.json | 1365 | 3 | 0 |
| story_19.json | 2730 | 3 | 0 |
| story_20.json | 1365 | 3 | 2 |
| story_20.json | 2730 | 3 | 0 |
| story_21.json | 1365 | 3 | 39 |
| story_21.json | 2730 | 3 | 0 |
| story_22.json | 1365 | 3 | 41 |
| story_22.json | 2730 | 3 | 0 |
| story_23.json | 1365 | 3 | 43 |
| story_23.json | 2730 | 3 | 0 |
| story_24.json | 1365 | 3 | 28 |
| story_24.json | 2730 | 3 | 0 |
| story_25.json | 1365 | 3 | 41 |
| story_25.json | 2730 | 3 | 0 |
| story_26.json | 1365 | 3 | 38 |
| story_26.json | 2730 | 3 | 0 |
| story_27.json | 1365 | 3 | 40 |
| story_27.json | 2730 | 3 | 0 |
| story_28.json | 1365 | 3 | 43 |
| story_28.json | 2730 | 3 | 0 |
| story_29.json | 1365 | 3 | 41 |
| story_29.json | 2730 | 3 | 0 |
| story_30.json | 1365 | 3 | 40 |
| story_30.json | 2730 | 3 | 0 |

## 6. 后续引用成本：索引被驱逐为长字面量的字段

共 2523 个字段在 A 侧为索引表示、在 B 侧退化为字面量。下表列出字节差额最大的 10 个（保留的是该次引用的实际线上成本）：

| story | seqno | 字段 | A 字节 | B 字节 | B−A | B 侧表示 | B 侧值 Huffman |
|---|---:|---|---:|---:|---:|---|---|
| story_20.json | 78 | `cookie: pid=v3:1351947306477664316206054; k=10.35.101.12` | 1 | 206 | 205 | literal-incremental | True |
| story_20.json | 129 | `cookie: pid=v3:1351947306477664316206054; k=10.35.101.12` | 1 | 206 | 205 | literal-incremental | True |
| story_27.json | 74 | `p3p: policyref="http://info.yahoo.com/w3c/p3p.xml", C` | 1 | 177 | 176 | literal-incremental | True |
| story_27.json | 76 | `p3p: policyref="http://info.yahoo.com/w3c/p3p.xml", C` | 1 | 177 | 176 | literal-incremental | True |
| story_27.json | 78 | `p3p: policyref="http://info.yahoo.com/w3c/p3p.xml", C` | 1 | 177 | 176 | literal-incremental | True |
| story_27.json | 80 | `p3p: policyref="http://info.yahoo.com/w3c/p3p.xml", C` | 1 | 177 | 176 | literal-incremental | True |
| story_27.json | 82 | `p3p: policyref="http://info.yahoo.com/w3c/p3p.xml", C` | 1 | 177 | 176 | literal-incremental | True |
| story_27.json | 84 | `p3p: policyref="http://info.yahoo.com/w3c/p3p.xml", C` | 1 | 177 | 176 | literal-incremental | True |
| story_27.json | 86 | `p3p: policyref="http://info.yahoo.com/w3c/p3p.xml", C` | 1 | 177 | 176 | literal-incremental | True |
| story_27.json | 88 | `p3p: policyref="http://info.yahoo.com/w3c/p3p.xml", C` | 1 | 177 | 176 | literal-incremental | True |
