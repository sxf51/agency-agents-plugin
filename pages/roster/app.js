/**
 * Page logic for the Agency Roster.
 *
 * The bridge is injected by the host, so it already exists when this file runs.
 * `ready()` resolves with the page context (user, locale, theme) and
 * `onContext()` fires again whenever the reader changes language or theme.
 *
 * Plain ES5-ish JavaScript, no build step and no imports: the page is served as
 * static files, and nothing is bundled.
 */
;(function () {
  var bridge = window.CapstonePluginPage

  // Text lives here, not in the HTML, because the reader can switch language
  // while the page is open. The backend only ever returns message codes.
  var TEXT = {
    en: {
      eyebrow: 'SPECIALIST DIRECTORY',
      session: 'CURRENT SESSION',
      promptLabel: 'SYSTEM PROMPT',
      directoryIndex: '01 / DIRECTORY',
      profileIndex: '02 / PROFILE',
      consultIndex: '03 / CONSULT',
      activityIndex: '04 / ACTIVITY',
      resultCount: 'shown',
      statsPersonas: 'personas',
      statsDivisions: 'divisions',
      statsStorage: 'Storage',
      title: 'Agency Roster',
      subtitle: 'Specialist personas, vendored from agency-agents (MIT).',
      searchTitle: 'Find a specialist',
      queryPlaceholder: 'What is the work about? e.g. react performance, tiktok launch',
      search: 'Search',
      allDivisions: 'All divisions',
      personaTitle: 'Persona',
      personaHint: 'Pick a persona from the results above.',
      download: 'Download markdown',
      askTitle: 'Ask this persona',
      questionPlaceholder: 'Ask the persona something',
      dryRun: 'Resolve only, no model call',
      ask: 'Ask',
      historyTitle: 'Your consultations',
      refresh: 'Refresh',
      remove: 'Delete',
      empty: 'No personas matched.',
      noHistory: 'Nothing consulted yet.',
      truncated: 'prompt clipped',
      routable: 'Routable as',
      notRoutable: 'This division is not registered as a subagent.',
      asked: 'Answered',
      resolved: 'Persona resolved',
      deleted: 'Record deleted',
      pickFirst: 'Pick a persona first',
      askFirst: 'Write a question first',
      // Backend message codes. Anything unmapped falls back to the raw code,
      // which is still more useful to a reader than a blank failure.
      catalog_unavailable: 'The persona catalog is missing on disk',
      agent_not_found: 'That persona is gone',
      agent_file_missing: 'That persona file is missing on disk',
      unknown_division: 'No such division',
      missing_slug: 'No persona was named',
      missing_question: 'Write a question first',
      missing_id: 'That record has no id',
      record_not_found: 'That record is gone',
      storage_unavailable: 'Storage is unavailable',
      executor_unavailable: 'The tool executor is unavailable',
      consult_timeout: 'The persona took too long to answer',
      consult_failed: 'The consultation failed',
      llm_unconfigured: 'No model is configured for this plugin',
      provider_error: 'The model provider refused the request',
    },
    zh: {
      eyebrow: '专家目录',
      session: '当前会话',
      promptLabel: '系统提示词',
      directoryIndex: '01 / 专家目录',
      profileIndex: '02 / 人格档案',
      consultIndex: '03 / 专家咨询',
      activityIndex: '04 / 咨询记录',
      resultCount: '位专家',
      statsPersonas: '位专家',
      statsDivisions: '个部门',
      statsStorage: '存储',
      title: '智能体名册',
      subtitle: '来自 agency-agents（MIT）的专家人格。',
      searchTitle: '找一位专家',
      queryPlaceholder: '这件事关于什么？例如 react 性能、tiktok 冷启动',
      search: '搜索',
      allDivisions: '全部部门',
      personaTitle: '人格',
      personaHint: '先从上面的结果里选一位。',
      download: '下载 markdown',
      askTitle: '向这位专家提问',
      questionPlaceholder: '想问什么',
      dryRun: '只解析人格，不调模型',
      ask: '提问',
      historyTitle: '我的咨询记录',
      refresh: '刷新',
      remove: '删除',
      empty: '没有匹配的人格。',
      noHistory: '还没有咨询记录。',
      truncated: '提示词已截断',
      routable: '可路由为',
      notRoutable: '该部门尚未注册为子智能体。',
      asked: '已作答',
      resolved: '人格已解析',
      deleted: '记录已删除',
      pickFirst: '请先选一位人格',
      askFirst: '请先写下问题',
      catalog_unavailable: '磁盘上找不到人格名册',
      agent_not_found: '该人格已不存在',
      agent_file_missing: '该人格文件在磁盘上已丢失',
      unknown_division: '没有这个部门',
      missing_slug: '没有指定人格',
      missing_question: '请先写下问题',
      missing_id: '该记录没有 id',
      record_not_found: '该记录已不存在',
      storage_unavailable: '存储不可用',
      executor_unavailable: '工具执行器不可用',
      consult_timeout: '这位专家回答超时',
      consult_failed: '咨询失败',
      llm_unconfigured: '本插件还没有配置可用的模型',
      provider_error: '模型服务商拒绝了这次请求',
    },
  }

  var locale = 'en'
  var selected = null
  // The last rows each list rendered. Kept because a language change has to
  // repaint text this page built itself - data-i18n only covers static markup,
  // and re-fetching on every toggle would be a request for nothing.
  var lastResults = []
  var lastHistory = []
  var lastDivisions = []
  var lastStats = null

  function t(key) {
    var table = TEXT[locale] || TEXT.en
    return table[key] || TEXT.en[key] || key
  }

  /** Turn a rejected bridge promise into text this reader can act on. */
  function explain(error) {
    var raw = (error && (error.message || error.code)) || String(error)
    // The backend answers {"status":"error","message":"<code>"}; the bridge
    // surfaces that message as the rejection reason.
    return t(String(raw).replace(/^Error:\s*/, '').trim())
  }

  function byId(id) {
    return document.getElementById(id)
  }

  function fail(error) {
    // notify() raises a toast in the dashboard shell, outside the iframe.
    bridge.notify(explain(error), 'error')
  }

  /** Re-render every translatable string. Called on load and on each change. */
  function paint() {
    var nodes = document.querySelectorAll('[data-i18n]')
    for (var index = 0; index < nodes.length; index += 1) {
      nodes[index].textContent = t(nodes[index].getAttribute('data-i18n'))
    }
    var placeholders = document.querySelectorAll('[data-i18n-placeholder]')
    for (var pIndex = 0; pIndex < placeholders.length; pIndex += 1) {
      placeholders[pIndex].placeholder = t(placeholders[pIndex].getAttribute('data-i18n-placeholder'))
    }
    // Anything rendered from data carries no data-i18n attribute, so the loops
    // above cannot reach it. Redraw those from what they last showed, or a
    // language switch leaves half the page in the old language.
    renderDivisions(lastDivisions)
    renderResults(lastResults)
    renderHistory(lastHistory)
    if (lastStats) renderStats(lastStats)
    if (selected) renderPersona(selected)
    else byId('persona-meta').textContent = t('personaHint')
  }

  function fillList(id, rows, emptyKey, renderRow) {
    var list = byId(id)
    if (!list) return
    list.innerHTML = ''
    if (!rows.length) {
      var empty = document.createElement('li')
      empty.className = 'muted'
      empty.textContent = t(emptyKey)
      list.appendChild(empty)
      return
    }
    rows.forEach(function (row) {
      list.appendChild(renderRow(row))
    })
  }

  // --- the roster ---------------------------------------------------------

  function loadDivisions() {
    return bridge.apiGet('divisions').then(function (rows) {
      renderDivisions(rows || [])
    }, fail)
  }

  function renderDivisions(rows) {
    lastDivisions = rows
    var select = byId('division')
    var current = select.value
    select.innerHTML = ''
    var all = document.createElement('option')
    all.value = ''
    all.textContent = t('allDivisions')
    select.appendChild(all)
    rows.forEach(function (row) {
      var option = document.createElement('option')
      option.value = row.key
      option.textContent = row.division + ' (' + row.agents + ')'
      select.appendChild(option)
    })
    select.value = current
  }

  function search() {
    // apiGet(endpoint, params) -> the host adds auth and the plugin prefix.
    return bridge
      .apiGet('agents', { q: byId('query').value, division: byId('division').value, limit: 20 })
      .then(function (rows) {
        renderResults(rows || [])
      }, fail)
  }

  function renderResults(rows) {
    lastResults = rows
    byId('result-count').textContent = rows.length + ' ' + t('resultCount')
    fillList('results', rows, 'empty', function (row) {
      var item = document.createElement('li')
      var label = document.createElement('button')
      label.className = 'result-button' + (selected && selected.agent && selected.agent.slug === row.slug ? ' active' : '')
      var icon = document.createElement('span')
      icon.className = 'result-emoji'
      icon.textContent = row.emoji || '✦'
      var copy = document.createElement('span')
      copy.className = 'result-copy'
      var name = document.createElement('span')
      name.className = 'result-name'
      name.textContent = row.name
      var detail = document.createElement('span')
      detail.className = 'result-detail'
      detail.textContent = row.division + (row.description ? ' · ' + row.description : '')
      var arrow = document.createElement('span')
      arrow.className = 'result-arrow'
      arrow.setAttribute('aria-hidden', 'true')
      arrow.textContent = '›'
      copy.appendChild(name)
      copy.appendChild(detail)
      label.appendChild(icon)
      label.appendChild(copy)
      label.appendChild(arrow)
      label.title = row.description
      label.onclick = function () {
        loadPersona(row.slug)
      }
      item.appendChild(label)
      return item
    })
  }

  function loadPersona(slug) {
    return bridge.apiGet('agent', { slug: slug }).then(function (body) {
      selected = body
      renderPersona(body)
      renderResults(lastResults)
    }, fail)
  }

  function renderPersona(body) {
    var agent = body.agent || {}
    byId('persona-name').textContent = (agent.emoji ? agent.emoji + ' ' : '') + (agent.name || t('personaTitle'))
    byId('persona-meta').textContent =
      agent.division + (agent.group ? ' / ' + agent.group : '') + ' · ' + agent.slug + ' · ' + agent.description
    byId('persona-vibe').textContent = agent.vibe || ''
    byId('persona-prompt').textContent = (agent.prompt || '') + (agent.truncated ? '\n\n… (' + t('truncated') + ')' : '')
    byId('persona-routing').textContent = body.subagent ? t('routable') + ' ' + body.subagent : t('notRoutable')
  }

  // --- consulting ---------------------------------------------------------

  function ask() {
    var question = byId('question').value.trim()
    if (!question) {
      bridge.notify(t('askFirst'), 'error')
      return
    }
    var slug = selected && selected.agent ? selected.agent.slug : ''
    var dryRun = byId('dry-run').checked
    byId('answer').textContent = '…'
    bridge.apiPost('consult', { agent: slug, question: question, dry_run: dryRun }).then(function (body) {
      var result = body.result || {}
      byId('answer').textContent = dryRun
        ? JSON.stringify({ agent: (result.agent || {}).name, would_send: result.would_send }, null, 2)
        : result.answer || result.report || ''
      bridge.notify(dryRun ? t('resolved') : t('asked'), 'success')
      loadStats()
      return loadHistory()
    }, function (error) {
      // Clear the placeholder as well as raising the toast: a box still
      // showing "…" after a refusal reads as a request that never came back.
      byId('answer').textContent = ''
      fail(error)
    })
  }

  function loadHistory() {
    return bridge.apiGet('recent', { limit: 10 }).then(function (rows) {
      renderHistory(rows || [])
    }, fail)
  }

  function renderHistory(rows) {
    lastHistory = rows
    fillList('history', rows, 'noHistory', function (row) {
      var item = document.createElement('li')
      var label = document.createElement('button')
      label.className = 'link wide'
      label.textContent = row.agent + ' · ' + row.question
      label.title = row.created
      label.onclick = function () {
        byId('answer').textContent = row.answer || ''
      }
      item.appendChild(label)

      var remove = document.createElement('button')
      remove.className = 'link danger'
      remove.textContent = t('remove')
      remove.onclick = function () {
        // apiDelete takes query params, not a body.
        bridge.apiDelete('history/delete', { id: row.id }).then(function () {
          bridge.notify(t('deleted'), 'success')
          loadStats()
          return loadHistory()
        }, fail)
      }
      item.appendChild(remove)
      return item
    })
  }

  function loadStats() {
    bridge.apiGet('stats').then(function (body) {
      lastStats = body
      renderStats(body)
    }, fail)
  }

  function renderStats(body) {
    byId('count').textContent = body.agents + ' ' + t('statsPersonas') + ' · ' + body.divisions + ' ' + t('statsDivisions')
    byId('backend').textContent = t('statsStorage') + ': ' + body.backend
  }

  // --- wiring -------------------------------------------------------------

  byId('search').onclick = search
  byId('query').onkeydown = function (event) {
    if (event.key === 'Enter') search()
  }
  byId('division').onchange = search
  byId('ask').onclick = ask

  byId('download').onclick = function () {
    if (!selected || !selected.agent) {
      bridge.notify(t('pickFirst'), 'error')
      return
    }
    // The host performs the save; the sandbox blocks a download the page starts
    // itself. Pass the filename so the save dialog shows it.
    bridge.download('export', { slug: selected.agent.slug }, selected.agent.slug + '.md').catch(fail)
  }

  byId('refresh-history').onclick = loadHistory

  // --- context ------------------------------------------------------------

  // ready() resolves with the page context; the host pushes it on frame load,
  // and this asks again in case the script ran after that push.
  bridge.ready().then(function (context) {
    // The context carries pluginName, pageName, pageTitle, locale, isDark and
    // i18n - deliberately not the user's identity. Ask the backend for that: it
    // knows who is calling from the host's own authentication.
    byId('who').textContent = context.pageTitle || context.pageName || '—'
    bridge.apiGet('ping').then(function (body) {
      byId('who').textContent = body.user
    }, fail)

    loadStats()
    loadDivisions().then(search)
    loadHistory()
  }, fail)

  // Fires on load and again on every theme or language change. The host also
  // sets data-theme on the document element, so CSS alone handles the colours;
  // only the text needs repainting here.
  bridge.onContext(function (context) {
    locale = context.locale === 'zh' ? 'zh' : 'en'
    paint()
  })

  paint()
})()
