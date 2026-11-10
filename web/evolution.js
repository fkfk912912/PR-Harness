/* Candidate evaluation, explicit approval and live shadow observations. */
class EvolutionUI {
  constructor() {
    this.data = null;
    this.selected = null;
    this.request = 0;
    this.pending = null;
    document.querySelector('#view-evolution').addEventListener('click', (event) => {
      const task = event.target.closest('[data-open-task]');
      if (task) { openTask(task.dataset.openTask); return; }
      const version = event.target.closest('[data-candidate]');
      if (version) { this.selected = Number(version.dataset.candidate); this.render(); return; }
      const action = event.target.closest('[data-release-action]');
      if (action) this.confirm(action.dataset.releaseAction);
    });
    document.querySelector('#release-cancel').addEventListener('click', () => this.dialog.close());
    this.dialog.addEventListener('cancel', event => {
      if (document.querySelector('#release-confirm-submit').disabled) event.preventDefault();
    });
    document.querySelector('#release-confirm-form').addEventListener('submit', (event) => {
      event.preventDefault(); this.submit();
    });
    setInterval(() => {
      if (!document.hidden && document.querySelector('#view-evolution.active') &&
          this.data?.deployment?.status === 'running' && !this.dialog.open) this.load();
    }, 6000);
  }

  get dialog() { return document.querySelector('#release-confirm'); }
  label(version) { return version == null ? '内置默认策略' : `V${version}`; }
  score(value) { return typeof value === 'number' ? value.toFixed(3) : '—'; }
  decision(value) {
    return ({shadow_ready: '评测通过 · 待确认', activated: '评测通过', rejected: '未通过评测',
      deferred: '暂缓处理'})[value] || value || '暂无结果';
  }
  reason(value) {
    const translations = {
      'candidate passed replay gates and is awaiting shadow/canary approval': '候选通过离线评测，等待确认试运行或启用。',
      'candidate improved on validation and passed the non-regression holdout gate': '验证集达到提升要求，保留集受保护指标没有退步。',
      'score improvement was below the configured threshold': '综合分提升未达到设定门槛',
      'a protected validation metric regressed': '验证集受保护指标退步',
      'a protected holdout metric regressed': '保留集受保护指标退步',
      'one or more baseline or candidate evaluations failed': '基准或候选的评测调用出现错误',
      'candidate prompt failed the deterministic safety/completeness gate': '提示词基本内容或格式检查未通过',
      'root-cause analysis produced no prompt change': '模型分析后没有提出新的提示词改动',
      'no new supported learning signal was found in unresolved feedback': '未处理反馈中没有可用的新改进信息，请先提交有依据的审查反馈',
      'candidate prompt is identical to the active version': '提交内容与当前正式版本相同',
      'candidate saved but no LLM provider is configured; replay evaluation was not run': '候选已保存；缺少模型配置，尚未执行回放评测',
      'candidate saved but the validation dataset is smaller than the activation minimum': '候选已保存；验证题数量不足，尚未执行评测',
      'candidate saved but the holdout dataset is smaller than the activation minimum': '候选已保存；保留题数量不足，尚未执行评测',
    };
    return String(value || '').split('; ').map(part => translations[part] || part).join('；');
  }
  async load(selectVersion) {
    const request = ++this.request;
    try {
      const [data, status, failures] = await Promise.all([
        api('/v1/evolution/console'), api('/v1/evolution/status'), api('/api/failures'),
      ]);
      if (request !== this.request) return;
      this.data = data;
      if (selectVersion != null) this.selected = selectVersion;
      if (!data.runs.some(r => r.candidate_version === this.selected))
        this.selected = data.runs[0]?.candidate_version ?? null;
      $('#evolution-status').textContent = formatJson(status);
      $('#evaluation-ready').textContent = `${status.validation_cases} 道验证题 · ${status.holdout_cases} 道保留题 · ${status.ready ? '可评测' : '条件未就绪'}`;
      $('#failure-list').innerHTML = (failures.cases || []).slice(0, 8).map(item => `
        <div class="task-row"><span class="task-copy"><strong>${escapeHtml(feedbackLabels[item.category] || item.category)}</strong>
        <small>${escapeHtml(item.payload?.note || '无说明')}</small></span>
        <span class="status status-neutral">${item.resolved ? '已解决' : '待处理'}</span></div>`).join('') ||
        '<p class="muted">暂无反馈。可以在任务中心提交误报、漏报或修复建议反馈。</p>';
      this.render();
    } catch (error) {
      if (request !== this.request) return;
      $('#evolution-notice').textContent = `读取演进状态失败：${error.message}。更新代码后请重启后端再刷新页面。`;
    }
  }
  async showResult(result) {
    $('#evolution-notice').textContent = !result.version
      ? `${this.decision(result.decision)}：本次没有创建新候选。${this.reason(result.reason)}`
      : `${this.decision(result.decision)}。${
      result.decision === 'shadow_ready' ? '候选已保存。选择下方候选，确认开始 Shadow 或直接启用。' :
      result.decision === 'activated' ? '新版本已经启用，后续新任务会使用它。' : this.reason(result.reason)}`;
    await this.load(result.version?.version);
    if (!result.version) $('#evolution-result').textContent = formatJson(result);
  }
  render() {
    if (!this.data) return;
    const {current_version: current, deployment: dep, runs, can_manage: manage} = this.data;
    $('#release-current').textContent = this.label(current);
    $('#release-current-note').textContent = '后续新建的正式审查任务使用此版本；历史任务保留原版本。';
    const state = ({running: '正在试运行', promoted: '已正式启用', stopped: '试运行已停止',
      rolled_back: '已回退'})[dep?.status] || '尚未开始试运行';
    $('#release-trial').textContent = dep ? `${state} · V${dep.candidate_version}` : state;
    $('#release-trial-note').textContent = dep?.status === 'running'
      ? `正式报告仍来自 ${this.label(current)}。${dep.auto_promote ? '已授权达到门槛后自动启用。' : '达到门槛后仍等待你确认启用。'}`
      : '先选择一个评测通过的候选，再确认试运行或启用。';
    $('#release-controls').innerHTML = `${dep?.status === 'running' ?
      `<button class="button secondary" data-release-action="stop" ${manage ? '' : 'disabled'}>停止试运行</button>` : ''}
      ${dep?.status === 'promoted' ? `<button class="button secondary" data-release-action="rollback" ${manage ? '' : 'disabled'}>回退到 ${this.label(dep.previous_version)}</button>` : ''}`;
    $('#evolution-history').innerHTML = runs.map(run => `
      <button class="candidate-row ${run.candidate_version === this.selected ? 'selected' : ''}" data-candidate="${run.candidate_version}" aria-pressed="${run.candidate_version === this.selected}">
        <span class="task-glyph">V${run.candidate_version}</span><span class="task-copy">
        <strong>${escapeHtml(this.decision(run.decision))}</strong>
        <small>候选 ${this.score(run.candidate_score)} / 基准 ${this.score(run.baseline_score)}</small>
        <small>${escapeHtml(formatTime(run.created_at))}${run.candidate_version === current ? ' · 正在使用' :
          dep?.status === 'running' && dep.candidate_version === run.candidate_version ? ' · Shadow 进行中' : ''}</small></span>
      </button>`).join('') || '<p class="empty-state">尚无候选。可从反馈生成，或手动提交提示词评测。</p>';
    this.renderCandidate(runs.find(r => r.candidate_version === this.selected));
    this.renderObservations();
    $('#auto-evolve').disabled = !manage;
  }
  renderCandidate(run) {
    const root = $('#candidate-detail');
    if (!run) { root.innerHTML = '<p class="empty-state">选择一个候选查看改动、成绩和下一步操作。</p>'; return; }
    const metrics = run.metrics || {};
    const dep = this.data.deployment;
    const active = this.data.current_version === run.candidate_version;
    const approved = ['shadow_ready', 'activated'].includes(run.decision) &&
      Object.keys(metrics.gates || {}).length > 0 && Object.values(metrics.gates).every(Boolean);
    const stale = run.baseline_version !== this.data.current_version && !active;
    const disabled = !approved || active || stale || !this.data.can_manage;
    const gates = {safety: '提示词基本检查', validation_dataset_ready: '验证题数量',
      holdout_dataset_ready: '保留题数量', evaluation_success: '评测调用正常',
      validation_improvement: '验证集达到提升要求', validation_non_regression: '验证集受保护指标不退步',
      holdout_non_regression: '保留集受保护指标不退步'};
    const rows = [['score', '综合分'], ['precision', '精确率'], ['recall', '召回率'],
      ['severity_accuracy', '严重程度达标比例'], ['high_severity_recall', '高风险召回率'], ['clean_accuracy', '正常代码不误报比例']];
    root.innerHTML = `<div class="candidate-heading"><div><p class="eyebrow">候选详情</p><h3>V${run.candidate_version}</h3></div>
      <span class="status status-neutral">${active ? '正在正式使用' : escapeHtml(this.decision(run.decision))}</span></div>
      <p class="muted">本次评测基准：${this.label(run.baseline_version)}。${active ? '已用于后续新任务。' : '评测历史与当前发布状态分别显示。'}</p>
      <div class="candidate-actions"><button class="button" data-release-action="shadow" ${disabled || dep?.status === 'running' ? 'disabled' : ''}>确认开始 Shadow</button>
      <button class="button secondary" data-release-action="activate" ${disabled ? 'disabled' : ''}>确认启用 V${run.candidate_version}</button></div>
      ${stale ? '<p class="release-warning">当前正式版本已变化；请基于当前版本重新评测这个候选。</p>' : ''}
      ${!approved ? '<p class="release-warning">该候选尚未通过全部离线门禁，不能启用。</p>' : ''}
      <div class="metric-scroll"><table class="release-table"><caption>离线成绩：基准 → 候选</caption><thead><tr><th>指标</th><th>Validation</th><th>Holdout</th></tr></thead><tbody>
      ${rows.map(([key, label]) => `<tr><th>${label}</th><td>${this.score(metrics.baseline?.[key])} → <b>${this.score(metrics.candidate?.[key])}</b></td>
      <td>${this.score(metrics.baseline_holdout?.[key])} → <b>${this.score(metrics.candidate_holdout?.[key])}</b></td></tr>`).join('')}</tbody></table></div>
      <p class="release-help">离线回放评测单个 LLM 审查器；Shadow 会执行完整多 Agent 链路。小题库单次满分不代表真实 PR 的稳定效果。</p>
      <details open><summary>门禁与处理原因</summary><ul class="gate-list">${Object.entries(gates).map(([key, label]) =>
        `<li class="${metrics.gates?.[key] === true ? 'gate-pass' : metrics.gates?.[key] === false ? 'gate-fail' : ''}">${metrics.gates?.[key] === true ? '通过' : metrics.gates?.[key] === false ? '未通过' : '未执行'} · ${label}</li>`).join('')}</ul>
      <p class="muted">${escapeHtml(this.reason(metrics.reason))}</p></details>
      <details open><summary>提示词改动</summary><pre class="prompt-diff">${escapeHtml(run.prompt_diff || '提示词没有文本变化')}</pre></details>
      <details><summary>完整候选提示词与运行配置</summary><pre>${escapeHtml(run.candidate_prompt)}</pre>
      <pre>${escapeHtml(formatJson(metrics.structured_candidate?.candidate || {}))}</pre></details>`;
    $('#evolution-result').textContent = formatJson(run);
  }
  renderObservations() {
    const dep = this.data.deployment;
    const observations = this.data.observations || [];
    const samples = dep?.shadow_samples || 0;
    $('#shadow-progress-label').textContent = dep ? `已完成 ${samples} / 目标 ${dep.min_samples} 次对照` : '尚无试运行样本';
    $('#shadow-progress').value = samples;
    $('#shadow-progress').max = Math.max(dep?.min_samples || 1, samples);
    $('#shadow-stats').innerHTML = [
      ['候选执行错误', samples ? `${dep.shadow_errors} / ${samples}` : '暂无'],
      ['结论存在差异', samples ? `${dep.disagreements} / ${samples}` : '暂无'],
      ['自动启用', dep?.auto_promote ? '已授权' : '等待人工确认'],
    ].map(([label, value]) => `<div><small>${label}</small><strong>${value}</strong></div>`).join('');
    $('#release-observations').innerHTML = observations.slice(0, 20).map(item => {
      const a = item.primary || {}, b = item.candidate || {};
      return `<details class="shadow-observation"><summary>${item.candidate_failed ? '候选执行异常' : item.disagreement ? '审查结论有差异' : '审查结论一致'} <span>${escapeHtml(formatTime(item.created_at))}</span></summary>
        <div class="metric-scroll"><table class="release-table"><thead><tr><th>对比</th><th>正式 ${this.label(a.prompt_version)}</th><th>候选 ${this.label(b.prompt_version)}</th></tr></thead><tbody>
        <tr><th>问题数量</th><td>${a.findings?.length || 0}</td><td>${b.findings?.length || 0}</td></tr>
        <tr><th>Token</th><td>${a.total_tokens || 0}</td><td>${b.total_tokens || 0}</td></tr>
        <tr><th>耗时</th><td>${((a.duration_ms || 0) / 1000).toFixed(1)} 秒</td><td>${((b.duration_ms || 0) / 1000).toFixed(1)} 秒</td></tr>
        </tbody></table></div><div class="candidate-actions">
        <button class="link-button" data-open-task="${escapeHtml(a.task_id)}">查看正式任务</button>
        <button class="link-button" data-open-task="${escapeHtml(b.task_id)}">查看候选任务</button></div>
        ${b.error ? `<p class="release-warning">${escapeHtml(b.error)}</p>` : ''}
        <details><summary>双方具体审查结论</summary><pre>${escapeHtml(formatJson({正式: a.findings, 候选: b.findings}))}</pre></details></details>`;
    }).join('') || '<p class="muted">开始 Shadow 后，新建审查任务会按所选比例产生实际对照记录。仅刷新页面不会产生样本。</p>';
  }
  confirm(action) {
    const run = this.data?.runs.find(r => r.candidate_version === this.selected);
    if (['shadow', 'activate'].includes(action) && !run) return;
    const dep = this.data.deployment;
    const version = run?.candidate_version;
    this.pending = {action, version, expected_current_version: this.data.current_version,
      expected_release_id: dep?.release_id ?? null};
    $('#release-confirm-title').textContent = ({shadow: `开始 V${version} 的 Shadow 试运行`,
      activate: `正式启用 V${version}`, stop: '停止试运行', rollback: `回退到 ${this.label(dep?.previous_version)}`})[action];
    $('#release-confirm-description').textContent = ({
      shadow: `新审查仍向用户展示 ${this.label(this.data.current_version)} 的报告；选中的请求会额外运行 V${version}，增加模型调用和 Token。候选结果单独保存，不回评到 GitHub。`,
      activate: `后续新任务将正式使用 V${version}，现有任务保留原版本。${dep?.shadow_samples ? '请先核对下方试运行记录。' : '目前尚无 Shadow 样本，这是直接启用。'}你可以通过回退恢复当前策略。`,
      stop: '停止为新请求创建影子任务；正在执行的候选任务会结束。正式报告继续使用当前版本。',
      rollback: `后续新任务恢复 ${this.label(dep?.previous_version)}，正在执行的任务保留原版本。`,
    })[action];
    $('#shadow-options').classList.toggle('hidden', action !== 'shadow');
    $('#release-confirm-error').textContent = '';
    $('#release-confirm-submit').textContent = action === 'shadow' ? '确认开始试运行' : action === 'activate' ? '确认正式启用' : '确认操作';
    this.dialog.showModal();
  }
  async submit() {
    const button = $('#release-confirm-submit');
    const payload = {...this.pending, confirmed: true};
    if (payload.action === 'shadow') Object.assign(payload, {
      shadow_percent: Number($('#shadow-percent').value), min_samples: Number($('#shadow-min-samples').value),
      max_error_rate: Number($('#shadow-error-budget').value) / 100,
      max_disagreement_rate: Number($('#shadow-disagreement-budget').value) / 100,
      auto_promote: $('#shadow-auto-promote').checked,
    });
    setButtonBusy(button, true, '正在保存…');
    $('#release-cancel').disabled = true;
    try {
      await api('/v1/evolution/releases', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      this.dialog.close();
      await this.load();
      $('#evolution-notice').textContent = ({shadow: 'Shadow 已开始。请创建新审查任务来积累对照样本。',
        activate: `V${payload.version} 已正式启用；可在任务中心确认新任务的版本。`, stop: '已停止新增试运行任务。', rollback: '已回退，后续新任务采用恢复后的版本。'})[payload.action];
    } catch (error) { $('#release-confirm-error').textContent = error.message; }
    finally { setButtonBusy(button, false); $('#release-cancel').disabled = false; }
  }
  renderTask(task) {
    const input = task.input || {};
    const shadow = input.source === 'shadow';
    const root = $('#task-version-banner');
    root.classList.remove('hidden');
    root.textContent = `${shadow ? 'Shadow 候选审查' : '正式审查'} · ${this.label(input.prompt_version)}${input.shadow_task_id ? ' · 已安排独立 Shadow 对照' : ''}`;
  }
}
const evolutionUI = new EvolutionUI();
