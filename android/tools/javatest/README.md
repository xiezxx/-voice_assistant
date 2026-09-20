# 移植比对：Java 端侧逻辑 vs Python 原件

端侧语音要在手机上重写三块逻辑（句子切分、VAD 判定、工具调用组配对），
它们全是**踩坑踩出来的**——18 字逗号切分是为了降首响延迟、0.8s 尾静音是调出来的、
工具组配对那 40 行修的是「永久 400」死循环。移植时最怕"顺手改写"，所以这里
**拿电脑上的 Python 原件当标准答案，Java 逐组对齐**。

## 怎么跑

```bash
cd android/tools/javatest
python gen_cases.py        # 生成 cases/*.tsv（用 Python 原件算参考结果）
javac -encoding UTF-8 -d out ../../app/src/main/java/com/xiaoyin/remote/local/*.java Compare.java
java -Dfile.encoding=UTF-8 -cp out Compare
```

预期输出：

```
句子切分：63 组
采集器：124 组
打断检测：63 组
工具组配对：8 组

比对结果：258 组通过，0 组不一致

Java 移植与 Python 原件行为一致 ✅
```

## 覆盖了什么

| 用例 | 组数 | 覆盖 |
|---|---|---|
| `speech.tsv` | 63 | 句末符立即切、18 字逗号提前切、半角逗号、换行当句末符、连续标点、无标点残余、空串、超长句；每种输入还按「整块／逐字／三字」三种分块方式各跑一遍，外加随机分块 |
| `utterance.tsv` | 124 | 全程安静、说完即停、只一个字、一直说不撞上限、以及 120 组随机电平序列（四种门限） |
| `barge.tsv` | 63 | 连续 3 块判打断、中间断裂要重新计数、以及 60 组随机序列 |
| `messages.tsv` | 8 | 完整工具组、孤儿 tool 消息、没凑齐的组、结尾裸调用、两组连续调用、空内容、未知角色 |

## 比对过程中真抓出来的 bug（留着当教训）

1. **`Matcher.find(pos)` 越界** —— Python 的 `re.search(s, pos)` 在 `pos` 超出长度时返回 `None`，
   Java 的 `Matcher.find(pos)` 直接抛 `IndexOutOfBoundsException`。缓冲区不足 18 字时就走这条路。
2. **`split("|")` 是正则** —— Java 的 `String.split` 收正则，`|` 是"或"，会把每个字符都切开，
   症状是消息全被过滤光。要用 `Pattern.quote`。
3. 两个脚手架自己的坑：TSV 里换行没转义（会被拆成两行）、Windows 写文件默认 `\r\n` 让 Java 读到行尾多一个 `\r`。

> 这三条都是**不看输出就发现不了**的类型——所以这套比对不是形式主义，是真省调试轮次。
