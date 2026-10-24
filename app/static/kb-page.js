'use strict';

(function () {
  // ===================== 常量 =====================

  // 状态提示默认文案
  var HINT_DEFAULT = '本地知识库即时检索 · 不消耗任何模型额度 · 结果带出处可核对';
  // 请求进行中的提示文案
  var HINT_LOADING = '正在本地检索…';
  // 后端未返回 message 时的兜底错误文案
  var ERROR_FALLBACK = '检索失败，请稍后再试。';
  // 输入防抖间隔（毫秒）
  var DEBOUNCE_MS = 350;
  // 错误提示自动隐藏时间（毫秒）
  var ERROR_HIDE_MS = 4000;

  // 搜索结果类型 → 中文标签
  var KIND_LABELS = {
    chapter: '章节',
    term: '名词卡',
    link: '链接'
  };

  // ===================== 页面已有 DOM 节点 =====================

  var form = document.getElementById('kb-page-search');
  var input = document.getElementById('kb-page-input');
  var hint = document.getElementById('kb-page-hint');
  var errorBox = document.getElementById('kb-error');
  var resultsBox = document.getElementById('kb-results');
  var chaptersBox = document.getElementById('kb-chapters');
  var termsTopicsBar = document.getElementById('kb-terms-topics');
  var termsGrid = document.getElementById('kb-terms');
  var linksTopicsBar = document.getElementById('kb-links-topics');
  var linksList = document.getElementById('kb-links-list');

  // ===================== 运行时状态 =====================

  // 各请求线的 AbortController，用于取消过期请求
  var overviewController = null;
  var searchController = null;
  var termsController = null;
  var linksController = null;

  // 输入防抖计时器
  var debounceTimer = null;
  // 错误自动隐藏计时器
  var errorTimer = null;

  // ===================== 通用工具 =====================

  // 创建元素（只用 textContent，禁止 innerHTML 拼接数据）
  function createEl(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  // 清空容器的所有子节点
  function clearChildren(container) {
    if (!container) return;
    while (container.firstChild) {
      container.removeChild(container.firstChild);
    }
  }

  // 平滑滚动到指定节点
  function scrollToNode(node) {
    if (node && typeof node.scrollIntoView === 'function') {
      node.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  // 更新状态提示文案
  function setHint(text) {
    if (hint) {
      hint.textContent = text;
    }
  }

  // 显示错误信息，并在 4 秒后自动隐藏
  function showError(message) {
    if (!errorBox) return;
    errorBox.textContent = message || ERROR_FALLBACK;
    errorBox.hidden = false;
    if (errorTimer) {
      clearTimeout(errorTimer);
    }
    errorTimer = setTimeout(function () {
      errorBox.hidden = true;
    }, ERROR_HIDE_MS);
  }

  // 判断错误是否为主动取消请求导致（取消不视为失败）
  function isAbortError(err) {
    return !!err && (err.name === 'AbortError' || err.code === 20);
  }

  // 发起 GET 请求并解析 JSON；非 2xx 时从错误信封中取 message 抛出
  function fetchJson(url, controller) {
    var options = { method: 'GET' };
    if (controller) {
      options.signal = controller.signal;
    }
    return fetch(url, options).then(function (resp) {
      return resp.json().catch(function () {
        // 响应体不是合法 JSON，统一抛兜底错误
        throw new Error(ERROR_FALLBACK);
      }).then(function (data) {
        if (!resp.ok) {
          var msg = (data && data.error && data.error.message) || ERROR_FALLBACK;
          throw new Error(msg);
        }
        return data;
      });
    });
  }

  // ===================== B. 搜索 =====================

  // 构造一条搜索结果 article.kb-hit
  function buildHit(item) {
    var article = createEl('article', 'kb-hit');

    // 头部：类型标签 + 标题
    var head = createEl('div', 'kb-hit-head');
    var kindLabel = KIND_LABELS[item.kind] || item.kind || '结果';
    var kindSpan = createEl('span', 'kb-kind', kindLabel);
    // 通过 dataset 暴露 kind，便于 CSS 按类型着色
    kindSpan.dataset.kind = item.kind || '';
    head.appendChild(kindSpan);
    head.appendChild(createEl('strong', 'kb-hit-title', item.title || ''));
    article.appendChild(head);

    // 摘要片段
    article.appendChild(createEl('p', 'kb-hit-snippet', item.snippet || ''));

    // 出处元信息
    article.appendChild(
      createEl('div', 'meta', '出处：' + (item.source || '') + ' · ' + (item.topic || ''))
    );

    // url 非空时追加外链
    if (item.url) {
      var link = createEl('a', 'kb-hit-url', '打开项目 →');
      link.href = item.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      article.appendChild(link);
    }

    return article;
  }

  // 渲染搜索结果到 #kb-results
  function renderResults(data) {
    if (!resultsBox) return;
    clearChildren(resultsBox);

    var results = (data && data.results) || [];
    if (!results.length) {
      var query = (data && data.query) || '';
      resultsBox.appendChild(
        createEl('p', 'muted', '没有找到与「' + query + '」相关的内容，换个问法试试。')
      );
      resultsBox.hidden = false;
      return;
    }

    results.forEach(function (item) {
      resultsBox.appendChild(buildHit(item));
    });
    resultsBox.hidden = false;
  }

  // 执行一次搜索（自动取消上一次未完成的搜索）
  function runSearch(query) {
    query = (query || '').trim();
    // 空查询不发起请求
    if (!query) return;

    if (searchController) {
      searchController.abort();
    }
    searchController = new AbortController();
    var controller = searchController;

    setHint(HINT_LOADING);

    fetchJson('/api/kb/search?q=' + encodeURIComponent(query) + '&limit=20', controller)
      .then(function (data) {
        // 过期请求的响应直接丢弃
        if (controller.signal.aborted) return;
        renderResults(data);
        setHint(HINT_DEFAULT);
      })
      .catch(function (err) {
        if (isAbortError(err)) return;
        setHint(HINT_DEFAULT);
        showError(err && err.message);
      });
  }

  // ===================== C. 名词卡专区 =====================

  // 渲染名词卡卡片到 #kb-terms
  function renderTerms(terms) {
    if (!termsGrid) return;
    clearChildren(termsGrid);

    if (!terms.length) {
      termsGrid.appendChild(createEl('p', 'muted', '该主题下暂无名词卡。'));
      return;
    }

    terms.forEach(function (term) {
      var card = createEl('article', 'kb-term-card');
      // 类型徽章沿用全站 .kb-card-label 样式，和首页名词卡保持一致
      var badge = createEl('span', 'kb-card-label', '名词卡');
      badge.dataset.kind = 'term';
      card.appendChild(badge);
      card.appendChild(createEl('strong', 'kb-term-name', term.term || ''));
      card.appendChild(createEl('p', 'kb-term-def', term.definition || ''));
      if (term.analogy) {
        card.appendChild(createEl('p', 'kb-term-analogy', '类比：' + term.analogy));
      }
      if (term.pitfalls) {
        card.appendChild(createEl('p', 'kb-term-pitfall', '易错点：' + term.pitfalls));
      }
      card.appendChild(
        createEl('div', 'meta', '出处：' + (term.source || '') + ' · ' + (term.topic || ''))
      );
      termsGrid.appendChild(card);
    });
  }

  // 点击名词卡主题 chip：按主题直接取名词卡（不靠全文检索，避免被章节结果挤掉）
  function loadTerms(topic) {
    if (termsController) {
      termsController.abort();
    }
    termsController = new AbortController();
    var controller = termsController;

    setHint(HINT_LOADING);

    fetchJson('/api/kb/terms?topic=' + encodeURIComponent(topic || '') + '&limit=50', controller)
      .then(function (data) {
        if (controller.signal.aborted) return;
        renderTerms((data && data.terms) || []);
        setHint(HINT_DEFAULT);
      })
      .catch(function (err) {
        if (isAbortError(err)) return;
        setHint(HINT_DEFAULT);
        showError(err && err.message);
      });
  }

  // ===================== D. 链接专区 =====================

  // 渲染链接列表到 #kb-links-list
  function renderLinks(links) {
    if (!linksList) return;
    clearChildren(linksList);

    if (!links.length) {
      linksList.appendChild(createEl('p', 'muted', '该主题下暂无链接。'));
      return;
    }

    links.forEach(function (item) {
      var card = createEl('article', 'link-item');

      // 链接标题（新窗口打开）
      var anchor = createEl('a', null, item.title || item.url || '');
      anchor.href = item.url || '#';
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      card.appendChild(anchor);

      // 推荐理由（非空时才渲染）
      if (item.why) {
        card.appendChild(createEl('p', 'why', item.why));
      }

      // 主题元信息
      card.appendChild(createEl('div', 'meta', '主题：' + (item.topic || '')));

      linksList.appendChild(card);
    });
  }

  // 点击链接主题 chip：加载该主题链接并滚动到链接区
  function loadLinks(topic, shouldScroll) {
    if (linksController) {
      linksController.abort();
    }
    linksController = new AbortController();
    var controller = linksController;

    setHint(HINT_LOADING);

    fetchJson('/api/links?topic=' + encodeURIComponent(topic) + '&limit=30', controller)
      .then(function (data) {
        if (controller.signal.aborted) return;
        renderLinks((data && data.links) || []);
        setHint(HINT_DEFAULT);
        // 只有用户点主题 chip 时才滚动；首屏预填保持位置不动
        if (shouldScroll !== false) {
          scrollToNode(linksList);
        }
      })
      .catch(function (err) {
        if (isAbortError(err)) return;
        setHint(HINT_DEFAULT);
        showError(err && err.message);
      });
  }

  // ===================== A. 初始化：章节目录与主题 chips =====================

  // 渲染章节目录：details.browse-chapter > summary + ul.browse-sections
  function renderChapters(chapters) {
    if (!chaptersBox) return;
    clearChildren(chaptersBox);

    if (!chapters.length) {
      chaptersBox.appendChild(createEl('p', 'muted', '暂无章节内容。'));
      return;
    }

    chapters.forEach(function (chapter) {
      var details = createEl('details', 'browse-chapter');
      details.appendChild(createEl('summary', null, chapter.title || ''));

      var ul = createEl('ul', 'browse-sections');
      (chapter.sections || []).forEach(function (section) {
        var li = document.createElement('li');
        var btn = createEl('button', 'browse-section-link', section);
        btn.type = 'button';
        btn.addEventListener('click', function () {
          // 用小节名检索，同步到搜索框，并滚动到结果区
          if (input) {
            input.value = section;
          }
          runSearch(section);
          scrollToNode(resultsBox);
        });
        li.appendChild(btn);
        ul.appendChild(li);
      });

      details.appendChild(ul);
      chaptersBox.appendChild(details);
    });
  }

  // 渲染一组主题 chips，点击时触发 onPick(topic)
  function renderTopicChips(container, topics, onPick) {
    if (!container) return;
    clearChildren(container);

    (topics || []).forEach(function (topic, index) {
      var chip = createEl('button', 'topic-chip', topic);
      chip.type = 'button';
      if (index === 0) {
        chip.classList.add('active');
      }
      chip.addEventListener('click', function () {
        // 同一组 chips 只保留一个选中态
        Array.prototype.forEach.call(container.querySelectorAll('.topic-chip'), function (el) {
          el.classList.remove('active');
        });
        chip.classList.add('active');
        onPick(topic);
      });
      container.appendChild(chip);
    });
  }

  // 拉取 overview 并渲染目录与两组 chips
  function initOverview() {
    if (overviewController) {
      overviewController.abort();
    }
    overviewController = new AbortController();

    setHint(HINT_LOADING);

    fetchJson('/api/kb/overview', overviewController)
      .then(function (data) {
        renderChapters((data && data.chapters) || []);
        renderTopicChips(termsTopicsBar, (data && data.terms_topics) || [], loadTerms);
        renderTopicChips(linksTopicsBar, (data && data.links_topics) || [], function (topic) {
          loadLinks(topic, true);
        });
        setHint(HINT_DEFAULT);
        // 首次进入先预填内容，避免两块专区是空的
        var firstTermTopic = ((data && data.terms_topics) || [])[0];
        if (firstTermTopic) {
          loadTerms(firstTermTopic);
        }
        loadLinks('', false);
      })
      .catch(function (err) {
        // 目录加载失败也要清掉「加载中…」占位
        clearChildren(chaptersBox);
        if (chaptersBox) {
          chaptersBox.appendChild(createEl('p', 'muted', '目录加载失败。'));
        }
        setHint(HINT_DEFAULT);
        showError(err && err.message);
      });
  }

  // ===================== E/F. 事件绑定：submit + 防抖输入 =====================

  // 表单提交：立即检索（取消未触发的防抖）
  if (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (debounceTimer) {
        clearTimeout(debounceTimer);
        debounceTimer = null;
      }
      runSearch(input ? input.value : '');
    });
  }

  // 输入防抖：停止输入 350ms 后自动检索
  if (input) {
    input.addEventListener('input', function () {
      if (debounceTimer) {
        clearTimeout(debounceTimer);
      }
      debounceTimer = setTimeout(function () {
        debounceTimer = null;
        runSearch(input.value);
      }, DEBOUNCE_MS);
    });
  }

  // ===================== 启动 =====================

  initOverview();
})();
