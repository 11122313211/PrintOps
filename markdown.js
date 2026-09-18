/* PrintOps 聊天气泡的轻量 Markdown 渲染器：零依赖、离线可用。
   设计取舍：
   - 先转义、再解析：原始 HTML 永远只当文字显示，模型或用户输入无法注入标签；
   - 只覆盖回复里真实出现的语法（标题、列表、表格、代码、引用、链接、加粗），
     不做完整 Markdown 兼容，也不支持 "_斜体_"，避免误伤 a_b_c.pdf 这类文件名；
   - 容忍未闭合标记：输出被截断时余下文字仍按纯文本展示，不会整段吞掉。 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.PrintOpsMarkdown = api;
})(typeof globalThis !== "undefined" ? globalThis : null, function () {
  "use strict";

  const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const TOKEN = "\u0000";
  const TOKEN_PATTERN = new RegExp(TOKEN + "(\\d+)" + TOKEN, "g");

  const HEADING = /^(#{1,6})[ \t]+(.*)$/;
  const BULLET = /^[ \t]{0,3}([-*+])[ \t]+(.*)$/;
  const ORDERED = /^[ \t]{0,3}(\d{1,3})[.)][ \t]+(.*)$/;
  const QUOTE = /^[ \t]{0,3}>[ \t]?(.*)$/;
  const FENCE = /^[ \t]{0,3}(`{3,}|~{3,})[ \t]*(.*)$/;
  const RULE = /^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const LANGUAGE = /^[A-Za-z0-9+#._-]{1,24}$/;
  const DIVIDER_CELL = /^:?-{2,}:?$/;

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/[&<>"']/g, (character) => ESCAPE_MAP[character]);
  }

  /* 已转义文本里的引号是 &quot;，不会截断 href 属性；这里只做协议白名单。 */
  function safeUrl(value) {
    const url = String(value === null || value === undefined ? "" : value).trim();
    return /^(?:https?:\/\/|mailto:)[^\s]+$/i.test(url) ? url : "";
  }

  function renderInline(source) {
    const tokens = [];
    const stash = (html) => {
      tokens.push(html);
      return TOKEN + (tokens.length - 1) + TOKEN;
    };
    let text = escapeHtml(source);
    text = text.replace(/`([^`\n]+)`/g, (match, code) => stash("<code>" + code + "</code>"));
    text = text.replace(/\[([^\]\n]+)\]\(([^()\s]+)\)/g, (match, label, href) => {
      const url = safeUrl(href);
      return url ? stash('<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>") : match;
    });
    text = text.replace(/\*\*([^\s*](?:[^*]*[^\s*])?)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/(^|[\s(（])\*([^\s*](?:[^*]*[^\s*])?)\*(?=$|[\s)）,.、。;；:：!！?？])/g, "$1<em>$2</em>");
    text = text.replace(/~~([^\s~](?:[^~]*[^\s~])?)~~/g, "<del>$1</del>");
    for (let guard = 0; guard < 6 && text.indexOf(TOKEN) >= 0; guard += 1) {
      text = text.replace(TOKEN_PATTERN, (match, index) => {
        const token = tokens[Number(index)];
        return token === undefined ? "" : token;
      });
    }
    return text;
  }

  function splitRow(line) {
    let value = line.trim();
    if (value.startsWith("|")) value = value.slice(1);
    if (value.endsWith("|")) value = value.slice(0, -1);
    return value.split("|").map((cell) => cell.trim());
  }

  function isTableDivider(line) {
    const cells = splitRow(line);
    return cells.length > 0 && cells.every((cell) => DIVIDER_CELL.test(cell));
  }

  function isTableStart(lines, index) {
    if (index + 1 >= lines.length) return false;
    return Boolean(lines[index].trim()) && lines[index].includes("|")
      && lines[index + 1].includes("|") && isTableDivider(lines[index + 1]);
  }

  function startsBlock(lines, index) {
    const line = lines[index];
    return Boolean(FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line)
      || BULLET.test(line) || ORDERED.test(line) || isTableStart(lines, index));
  }

  function renderTable(lines, index, blocks) {
    const header = splitRow(lines[index]);
    const aligns = splitRow(lines[index + 1]).map((cell) => {
      const left = cell.startsWith(":");
      const right = cell.endsWith(":");
      if (left && right) return "md-center";
      if (right) return "md-right";
      if (left) return "md-left";
      return "";
    });
    let cursor = index + 2;
    const rows = [];
    while (cursor < lines.length && lines[cursor].trim() && lines[cursor].includes("|")) {
      const cells = splitRow(lines[cursor]);
      rows.push(header.map((cell, position) => (cells[position] === undefined ? "" : cells[position])));
      cursor += 1;
    }
    const renderCell = (tag, value, position) => {
      const align = aligns[position];
      return "<" + tag + (align ? ' class="' + align + '"' : "") + ">" + renderInline(value) + "</" + tag + ">";
    };
    const head = header.map((value, position) => renderCell("th", value, position)).join("");
    const body = rows.map((row) => "<tr>" + row.map((value, position) => renderCell("td", value, position)).join("") + "</tr>").join("");
    blocks.push('<div class="md-table"><table><thead><tr>' + head + "</tr></thead><tbody>" + body + "</tbody></table></div>");
    return cursor;
  }

  function renderBlocks(lines) {
    const blocks = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }
      const fence = line.match(FENCE);
      if (fence) {
        const marker = fence[1][0];
        const closer = new RegExp("^[ \\t]{0,3}" + (marker === "`" ? "`{3,}" : "~{3,}") + "[ \\t]*$");
        const code = [];
        index += 1;
        while (index < lines.length && !closer.test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const language = fence[2].trim().split(/\s+/)[0];
        const className = language && LANGUAGE.test(language) ? ' class="language-' + language + '"' : "";
        blocks.push("<pre><code" + className + ">" + escapeHtml(code.join("\n")) + "</code></pre>");
        continue;
      }
      const heading = line.match(HEADING);
      if (heading) {
        const level = Math.min(heading[1].length, 6);
        blocks.push("<h" + level + ">" + renderInline(heading[2].trim()) + "</h" + level + ">");
        index += 1;
        continue;
      }
      if (RULE.test(line)) {
        blocks.push("<hr />");
        index += 1;
        continue;
      }
      if (isTableStart(lines, index)) {
        index = renderTable(lines, index, blocks);
        continue;
      }
      const quote = line.match(QUOTE);
      if (quote) {
        const inner = [];
        while (index < lines.length) {
          const current = lines[index].match(QUOTE);
          if (current) {
            inner.push(current[1]);
            index += 1;
            continue;
          }
          if (inner.length && lines[index].trim() && !startsBlock(lines, index)) {
            inner.push(lines[index]);
            index += 1;
            continue;
          }
          break;
        }
        blocks.push("<blockquote>" + renderBlocks(inner).join("") + "</blockquote>");
        continue;
      }
      const bullet = line.match(BULLET);
      const ordered = bullet ? null : line.match(ORDERED);
      if (bullet || ordered) {
        const isOrdered = Boolean(ordered);
        const items = [];
        let start = 1;
        while (index < lines.length) {
          const currentBullet = lines[index].match(BULLET);
          const currentOrdered = !currentBullet && isOrdered ? lines[index].match(ORDERED) : null;
          const matched = isOrdered ? currentOrdered : currentBullet;
          if (matched) {
            if (!items.length && isOrdered) start = Number(currentOrdered[1]) || 1;
            items.push([matched[2]]);
            index += 1;
            continue;
          }
          if (items.length && /^[ \t]+\S/.test(lines[index])) {
            items[items.length - 1].push(lines[index].trim());
            index += 1;
            continue;
          }
          break;
        }
        const tag = isOrdered ? "ol" : "ul";
        const attribute = isOrdered && start !== 1 ? ' start="' + start + '"' : "";
        const body = items.map((item) => {
          const [first, ...rest] = item;
          return "<li>" + renderInline(first)
            + rest.map((extra) => "<br />" + renderInline(extra)).join("") + "</li>";
        }).join("");
        blocks.push("<" + tag + attribute + ">" + body + "</" + tag + ">");
        continue;
      }
      const paragraph = [];
      while (index < lines.length && lines[index].trim() && !startsBlock(lines, index)) {
        paragraph.push(lines[index]);
        index += 1;
      }
      if (paragraph.length) blocks.push("<p>" + paragraph.map(renderInline).join("<br />") + "</p>");
      else index += 1;
    }
    return blocks;
  }

  function render(source) {
    const normalized = String(source === null || source === undefined ? "" : source).replace(/\r\n?/g, "\n");
    if (!normalized.trim()) return "";
    return renderBlocks(normalized.split("\n")).join("");
  }

  return { render: render, renderInline: renderInline, escapeHtml: escapeHtml };
});
