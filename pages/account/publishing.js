const $ = (id) => document.getElementById(id);
const labels = { pending: "待审核", sending: "发送中", sent: "已发送", dry_run: "模拟运行", rejected: "已拒绝", failed: "发送失败", send_unknown: "发送结果未知" };
const statusText = (status) => labels[status] || status || "未知状态";
const sourceLabels = { dashboard: "管理面板", notification: "通知回复", proactive: "主动评论", legacy_candidate: "历史主动候选" };

export function createPublishing({ bridge, busy, confirmAction, metric, toast, formatTime, isUnloaded }) {
  let revision = 0;
  const reviewRevisions = { post: 0, comment: 0 };
  const actions = new Set();
  let submitting = false;
  let submittedText = null;
  let uncertain = false;
  let defaultsApplied = false;

  function configure(config) {
    const policy = config.publishing || {};
    const select = $("publish-profile");
    const previous = select.value || policy.profile_id || "default";
    select.replaceChildren();
    for (const profile of config.profiles || []) {
      const option = document.createElement("option");
      option.value = profile.profile_id;
      option.textContent = `${profile.display_name || profile.profile_id} · ${profile.profile_id}`;
      select.append(option);
    }
    select.value = [...select.options].some((option) => option.value === previous) ? previous : policy.profile_id || "default";
    if (!defaultsApplied) {
      $("post-topics").value = policy.default_topic_ids || "";
      $("post-view-limit").value = String(policy.view_limit || 1);
      defaultsApplied = true;
    }
    $("publishing-policy").textContent = `默认档案：${policy.profile_id || "default"} · 模拟运行：${policy.dry_run !== false ? "开启" : "关闭"} · 发帖审核：${policy.post_review_required !== false ? "开启" : "关闭"} · 评论审核：${policy.comment_review_required !== false ? "开启" : "关闭"}`;
  }

  async function loadStatus() {
    const version = ++revision;
    const data = await bridge.apiGet("publishing/status");
    if (isUnloaded() || version !== revision) return;
    $("publishing-status").replaceChildren(
      metric("默认发布档案", data.profile_id),
      metric("发布凭据", data.has_credentials ? "已配置" : "未登录"),
      metric("发布 UID", data.uid),
      metric("发布模拟运行", data.dry_run ? "开启" : "关闭"),
      metric("发帖审核", data.post_review_required ? "开启" : "关闭"),
      metric("评论审核", data.comment_review_required ? "开启" : "关闭"),
      ...Object.entries(data.counts || {}).map(([key, value]) => metric(statusText(key),
        typeof value === "object" && value !== null ? JSON.stringify(value) : value)),
    );
  }

  function showResult(result, host) {
    const status = result.status || result.review?.status;
    const messages = {
      pending: "已加入审核队列，请前往对应的发帖审核或评论审核面板。",
      dry_run: "模拟运行完成，没有实际发送。",
      sent: "发送成功。",
      sending: "正在发送，请等待并刷新审核列表，不能重复提交。",
      rejected: "该审核项已拒绝，不会发送。",
      failed: "发送失败，服务端已明确拒绝。请查看错误信息并修正内容。",
      send_unknown: "发送结果未知：请到小黑盒核对目标内容，不能重发。",
    };
    if (!Object.hasOwn(messages, status)) {
      host.textContent = "未能确认提交结果。请先刷新审核列表并到小黑盒核对，不能直接重发。";
      host.dataset.tone = "warning";
      return "send_unknown";
    }
    host.textContent = messages[status];
    if (status === "failed" && (result.error || result.review?.error)) host.textContent += ` ${result.error || result.review.error}`;
    if (status === "dry_run") host.textContent += result.review?.status === "pending"
      ? " 审核项仍保留为待审核。" : !result.review ? " 本次未生成审核项。" : "";
    if (result.review?.id) host.textContent += ` 审核编号：${result.review.id}`;
    host.dataset.tone = status === "failed" ? "error" : ["send_unknown", "sending"].includes(status) ? "warning" : "success";
    return status;
  }

  function reviewCard(item) {
    const card = document.createElement("article");
    card.className = "candidate";
    card.dataset.reviewId = String(item.id);
    const payload = item.payload || {};
    const heading = document.createElement("h3");
    heading.textContent = payload.title || (item.kind === "comment" ? `评论 · 帖子 ${payload.post_id || "—"}` : "帖子");
    const meta = document.createElement("p");
    const updateMeta = () => {
      meta.textContent = `档案 ${item.profile_id || "—"} · 账号 ${item.account_id || "—"} · ${statusText(item.status)} · 来源 ${sourceLabels[item.source] || item.source || "—"} · ${formatTime(item.created_at)} · 编号 ${item.id}`;
    };
    updateMeta();
    const target = document.createElement("p");
    target.textContent = item.kind === "comment"
      ? `目标帖子 ${payload.post_id || "—"} · 父评论 ${payload.parent_comment_id || "无"} · 根评论 ${payload.root_comment_id || "无"}`
      : `${payload.mode === "article" ? "长文" : "动态"} · ${payload.draft ? "草稿" : "发布"} · 话题 ${(payload.topic_ids || []).join("、") || "无"} · 可见范围 ${{1: "公开", 2: "粉丝可见", 3: "仅自己可见"}[payload.view_limit] || payload.view_limit || "默认"}`;
    const content = document.createElement("pre");
    content.className = "review-content";
    content.textContent = payload.text || "";
    card.append(heading, meta, target, content);
    if (item.error || item.result) {
      const detail = document.createElement("pre");
      detail.className = "review-content";
      detail.textContent = [item.error, item.result ? JSON.stringify(item.result, null, 2) : ""].filter(Boolean).join("\n");
      card.append(detail);
    }
    const resultNode = document.createElement("p");
    resultNode.setAttribute("role", "status");
    card.append(resultNode);
    if (item.status === "send_unknown") {
      resultNode.className = "review-warning";
      resultNode.textContent = "发送结果未知，请人工核对；不能批准、拒绝或重发。";
    }
    if (item.status !== "pending") return card;
    const row = document.createElement("div");
    row.className = "button-row";
    const approve = document.createElement("button");
    approve.textContent = "批准";
    const reject = document.createElement("button");
    reject.textContent = "拒绝";
    reject.className = "danger secondary";
    const key = `${item.kind}:${item.id}`;
    for (const [button, action] of [[approve, "approve"], [reject, "reject"]]) {
      button.type = "button";
      button.addEventListener("click", async () => {
        if (isUnloaded() || actions.has(key) || item.status !== "pending") return;
        actions.add(key);
        approve.disabled = reject.disabled = true;
        let attempted = false;
        try {
          if (action === "approve") {
            const accepted = await confirmAction(`确认批准档案 ${item.profile_id}（账号 ${item.account_id || "—"}）的${item.kind === "post" ? "帖子" : "评论"}？\n${target.textContent}\n${payload.text || ""}\n将按服务端当前审核与模拟运行设置处理。`, { confirmText: "批准并执行" });
            if (!accepted || isUnloaded()) return;
          }
          // Invalidate older list requests before a mutation can change this row.
          reviewRevisions[item.kind]++;
          attempted = true;
          const response = await bridge.apiPost(`publishing/reviews/${encodeURIComponent(item.id)}/${action}`, action === "approve" ? { confirm: true } : {});
          if (isUnloaded()) return;
          if (action === "approve") {
            const outcome = showResult(response, resultNode);
            item.status = response.review?.status || outcome;
          }
          else {
            item.status = "rejected";
            resultNode.textContent = "已拒绝。";
          }
          updateMeta();
          toast(resultNode.textContent);
          await Promise.all([loadReviews(item.kind), loadStatus()]).catch(() => {
            if (!isUnloaded()) toast("审核结果已记录，状态刷新失败，请手动刷新审核列表。");
          });
        } catch {
          if (!isUnloaded()) resultNode.textContent = attempted
            ? "操作结果未确认，请刷新审核列表并人工核对；不能直接重试发送。"
            : "无法完成操作，请检查 Dashboard 登录状态。";
        } finally {
          actions.delete(key);
          if (!isUnloaded()) approve.disabled = reject.disabled = attempted;
        }
      });
    }
    row.append(approve, reject);
    card.append(row);
    return card;
  }

  async function loadReviews(kind) {
    const version = ++reviewRevisions[kind];
    const result = await bridge.apiGet("publishing/reviews", { kind });
    if (isUnloaded() || version !== reviewRevisions[kind]) return;
    const items = result.items || [];
    const host = $(`${kind}-review-list`);
    host.replaceChildren(...items.map(reviewCard));
    if (!items.length) host.textContent = "暂无审核记录。";
  }

  function readSubmission() {
    const kind = $("publish-kind").value;
    const profile_id = $("publish-profile").value;
    const text = $("publish-text").value.trim();
    if (!profile_id || !text || !["post", "comment"].includes(kind)) throw new Error("请选择账号档案并填写正文");
    const payload = kind === "post" ? {
      title: $("post-title").value.trim(), text,
      topic_ids: [...new Set($("post-topics").value.split(/[\s,，]+/).filter(Boolean))],
      mode: $("post-mode").value, draft: $("post-draft").checked,
      view_limit: Number($("post-view-limit").value),
    } : { post_id: $("comment-post-id").value.trim(), text,
      parent_comment_id: $("comment-parent-id").value.trim(), root_comment_id: $("comment-root-id").value.trim() };
    if (kind === "comment" && !payload.post_id) throw new Error("请填写目标帖子 ID");
    if (kind === "post" && (!["dynamic", "article"].includes(payload.mode) || ![1, 2, 3].includes(payload.view_limit))) throw new Error("请选择有效帖子类型和可见范围");
    return { kind, profile_id, payload };
  }

  $("publish-kind").addEventListener("change", () => {
    const isPost = $("publish-kind").value === "post";
    $("post-fields").hidden = $("post-fields").disabled = !isPost;
    $("comment-fields").hidden = $("comment-fields").disabled = isPost;
  });
  $("publish-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || uncertain || isUnloaded()) return;
    let submission;
    try { submission = readSubmission(); }
    catch (error) { toast(error.message, "error"); return; }
    const signature = JSON.stringify(submission);
    if (submittedText === signature) { toast("这份内容已提交，请先检查审核列表或目标帖子。"); return; }
    submitting = true;
    $("publish-submit").disabled = true;
    let attempted = false;
    try {
      const accepted = await confirmAction(`确认提交档案 ${submission.profile_id} 的${submission.kind === "post" ? "帖子" : `评论（帖子 ${submission.payload.post_id}）`}？\n${submission.payload.title || ""}\n${submission.payload.text}\n${$("publishing-policy").textContent}\n最终处理方式以服务端当前设置为准。`, { confirmText: "确认提交" });
      if (!accepted || isUnloaded()) return;
      attempted = true;
      const response = await bridge.apiPost("publishing/submit", submission);
      if (isUnloaded()) return;
      submittedText = response.status === "dry_run" && !response.review ? null : signature;
      uncertain = ["send_unknown", "sending"].includes(showResult(response, $("publish-result")));
      // Refresh failures must not turn a confirmed send into a retryable submission.
      await Promise.all([loadReviews(submission.kind), loadStatus()]).catch(() => {
        if (!isUnloaded()) toast("提交结果已记录，状态刷新失败，请手动刷新审核列表。");
      });
    } catch {
      if (!isUnloaded()) {
        uncertain = attempted;
        $("publish-result").textContent = attempted
          ? "提交结果未确认：请先刷新审核列表并到小黑盒核对，不能重发。本页已停用提交。"
          : "无法完成操作，请检查 Dashboard 登录状态。";
        $("publish-result").dataset.tone = "warning";
      }
    } finally {
      submitting = false;
      if (!isUnloaded()) $("publish-submit").disabled = uncertain;
    }
  });
  for (const kind of ["post", "comment"]) {
    const button = $(`refresh-${kind}-reviews`);
    button.addEventListener("click", () => busy(button, () => loadReviews(kind)));
  }
  return { configure, loadStatus, loadReviews };
}
