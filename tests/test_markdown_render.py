"""Contract tests for markdown.js — the renderer behind chat bubbles and the composer preview.

markdown.js is the only place that turns model or user text into HTML, so the
first rule under test is that raw HTML never reaches the DOM as markup. The
renderer runs in the browser; these tests drive it through node so the contract
is checked without a browser, and skip cleanly when node is unavailable.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "markdown.js"
NODE = shutil.which("node")
EVENT_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
TAG = re.compile(r"<[^>]*>")
TAG_NAME = re.compile(r"^</?\s*([a-zA-Z0-9]+)")
ALLOWED_TAGS = {
    "p", "br", "strong", "em", "del", "code", "pre", "blockquote", "hr", "a",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "div", "span", "table", "thead", "tbody", "tr", "th", "td",
}


HARNESS = """
const markdown = require(process.argv[1]);
const inputs = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
process.stdout.write(JSON.stringify(inputs.map((value) => markdown.render(value))));
"""


CASES = [
    {
        "name": "空输入返回空串",
        "input": "",
        "contains": [],
        "excludes": ["<p>"],
    },
    {
        "name": "普通中文回复按段落渲染",
        "input": "已记录：500 份 A4 宣传册。\n\n还需要确认：交期。",
        "contains": ["<p>已记录：500 份 A4 宣传册。</p>", "<p>还需要确认：交期。</p>"],
        "excludes": ["**", "<script"],
    },
    {
        "name": "单换行不被合并",
        "input": "第一行\n第二行",
        "contains": ["第一行<br />第二行"],
        "excludes": [],
    },
    {
        "name": "标题与加粗",
        "input": "### 推荐方案\n**平衡档**：157g 哑粉纸",
        "contains": ["<h3>推荐方案</h3>", "<strong>平衡档</strong>"],
        "excludes": ["**"],
    },
    {
        "name": "无序列表",
        "input": "- 骑马钉\n- 胶装",
        "contains": ["<ul><li>骑马钉</li><li>胶装</li></ul>"],
        "excludes": [],
    },
    {
        "name": "任务列表展示印刷待确认项",
        "input": "- [x] 已确认数量\n- [ ] 待确认交期",
        "contains": [
            '<li class="md-task-item is-done"><span class="md-task-marker is-done" aria-hidden="true">✓</span>',
            '<li class="md-task-item"><span class="md-task-marker" aria-hidden="true">○</span>',
            "待确认交期",
        ],
        "excludes": ["[x]", "[ ]"],
    },
    {
        "name": "有序列表保留起始序号",
        "input": "3. 第三步\n4. 第四步",
        "contains": ['<ol start="3">', "<li>第三步</li>"],
        "excludes": [],
    },
    {
        "name": "表格按表结构渲染",
        "input": "| 项目 | 参数 |\n| --- | --- |\n| 纸张 | 157g |",
        "contains": ['<div class="md-table"><table>', "<th>项目</th>", "<td>157g</td>"],
        "excludes": ["| --- |"],
    },
    {
        "name": "围栏代码块保留原文并转义",
        "input": "```python\nprint('<x>')\n```",
        "contains": ['<pre><code class="language-python">', "&lt;x&gt;"],
        "excludes": ["<x>"],
    },
    {
        "name": "行内代码转义",
        "input": "用 `a<b` 表示",
        "contains": ["<code>a&lt;b</code>"],
        "excludes": ["a<b"],
    },
    {
        "name": "引用块",
        "input": "> 出血一般留 3mm",
        "contains": ["<blockquote>", "出血一般留 3mm"],
        "excludes": ["&gt;"],
    },
    {
        "name": "http 链接放行并带 rel",
        "input": "[参考](https://example.com/a?b=1&c=2)",
        "contains": ['href="https://example.com/a?b=1&amp;c=2"', 'rel="noopener noreferrer"'],
        "excludes": ["javascript:"],
    },
    {
        "name": "链接属性中的引号保持转义",
        "input": "[带引号](https://example.com/a\"b?x=1)",
        "contains": ['href="https://example.com/a&quot;b?x=1"'],
        "excludes": ['href="https://example.com/a"b'],
    },
    {
        "name": "危险协议不生成链接",
        "input": "[点我](javascript:alert(1))",
        "contains": ["[点我](javascript:alert(1))"],
        "excludes": ["<a ", "href="],
    },
    {
        "name": "原始 HTML 只当文字",
        "input": "<img src=x onerror=alert(1)>",
        "contains": ["&lt;img src=x onerror=alert(1)&gt;"],
        "excludes": ["<img"],
    },
    {
        "name": "脚本标签被转义",
        "input": "<script>alert('x')</script>",
        "contains": ["&lt;script&gt;"],
        "excludes": ["<script"],
    },
    {
        "name": "下划线文件名不被当作斜体",
        "input": "上传文件：a_b_c.pdf",
        "contains": ["a_b_c.pdf"],
        "excludes": ["<em>"],
    },
    {
        "name": "未闭合加粗保持纯文本",
        "input": "**未闭合的加粗",
        "contains": ["**未闭合的加粗"],
        "excludes": ["<strong>"],
    },
    {
        "name": "未闭合围栏不丢内容",
        "input": "```\n半截输出",
        "contains": ["<pre>", "半截输出"],
        "excludes": [],
    },
    {
        "name": "单个星号算式不被当作强调",
        "input": "数量 2 * 3 = 6",
        "contains": ["2 * 3 = 6"],
        "excludes": ["<em>"],
    },
]


@unittest.skipIf(NODE is None, "未找到 node，跳过 markdown.js 渲染契约测试")
class MarkdownRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outputs = cls.render_all([case["input"] for case in CASES])

    @staticmethod
    def render_all(inputs):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump(inputs, handle, ensure_ascii=False)
            handle.close()
            completed = subprocess.run(
                [NODE, "-e", HARNESS, str(RENDERER), handle.name],
                capture_output=True, text=True, timeout=60)
        finally:
            Path(handle.name).unlink(missing_ok=True)
        if completed.returncode != 0:
            raise AssertionError("markdown.js 渲染失败：%s" % completed.stderr.strip())
        return json.loads(completed.stdout)

    def test_render_matches_contract(self):
        for case, output in zip(CASES, self.outputs):
            with self.subTest(case=case["name"]):
                for fragment in case["contains"]:
                    self.assertIn(fragment, output)
                for fragment in case["excludes"]:
                    self.assertNotIn(fragment, output)

    def test_output_never_contains_raw_html_tags(self):
        for case, output in zip(CASES, self.outputs):
            with self.subTest(case=case["name"]):
                for tag in TAG.findall(output):
                    name = TAG_NAME.match(tag)
                    self.assertIsNotNone(name, tag)
                    self.assertIn(name.group(1).lower(), ALLOWED_TAGS, tag)
                    self.assertIsNone(EVENT_HANDLER.search(tag), tag)
                plain = TAG.sub("", output)
                self.assertNotIn("<", plain, output)
                self.assertNotIn(">", plain, output)


if __name__ == "__main__":
    unittest.main()
