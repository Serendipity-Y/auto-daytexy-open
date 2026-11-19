document.documentElement.classList.add("js");

const postForm = async (url, token, values) => {
  const body = new FormData();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined) body.set(key, String(value));
  });
  const response = await fetch(url, {
    method: "POST",
    headers: { "X-Local-Request-Token": token },
    body,
  });
  const payload = await response.json().catch(() => ({ error: "服务器返回了无法解析的结果。" }));
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("服务器返回了无法解析的结果。");
  }
  if (!response.ok || payload.error) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
};

const appendFact = (container, label, value) => {
  const row = document.createElement("div");
  if (label === "关联规则") row.classList.add("preview-fact--wide");
  const term = document.createElement("span");
  const detail = document.createElement("strong");
  term.textContent = label;
  detail.textContent = value || "—";
  row.append(term, detail);
  container.append(row);
  return detail;
};

const initializeDayActions = () => {
  const panel = document.querySelector("[data-action-panel]");
  const dialog = document.querySelector("[data-confirm-dialog]");
  if (!panel || !dialog) return;

  const reportDate = panel.dataset.reportDate;
  const token = panel.dataset.localToken;
  const panelMessage = panel.querySelector("[data-action-message]");
  const dialogMessage = dialog.querySelector("[data-dialog-message]");
  const previewList = dialog.querySelector("[data-preview-list]");

  const setMessage = (element, message, isError = false) => {
    element.textContent = message;
    element.classList.toggle("is-error", isError);
  };

  const renderPreviews = (previews) => {
    previewList.replaceChildren();
    if (!previews.length) {
      setMessage(dialogMessage, "该日报没有可执行的对应内容。", true);
      return;
    }
    previews.forEach((preview) => {
      const item = document.createElement("article");
      item.className = "preview-item";
      const facts = document.createElement("div");
      facts.className = "preview-facts";
      appendFact(facts, "操作", preview.action_label);
      appendFact(facts, "日期", preview.report_date);
      appendFact(facts, "客户", preview.customer);
      if (preview.action === "onsite_service" && preview.work_type) {
        appendFact(facts, "工作类型", preview.work_type);
        if (preview.routing_rule) appendFact(facts, "关联规则", preview.routing_rule);
        if (preview.routing_target) {
          appendFact(facts, "关联工单", preview.routing_target.workorder_id);
          appendFact(facts, "工单创建时间", preview.routing_target.created_at);
        }
      }
      appendFact(
        facts,
        "工时",
        preview.hours ? `${preview.hours} 小时` : (preview.requires_hours ? "待填写" : "不适用")
      );

      const content = document.createElement("pre");
      content.className = "preview-content";
      content.textContent = preview.content;
      const controls = document.createElement("div");
      controls.className = "preview-controls";

      const reviewFields = {};
      let confirm;
      if (preview.action === "remote_workorder") {
        appendFact(facts, "来源", preview.includes_main_work ? "主服务" : "远程支持");
        appendFact(facts, "日报处理结果", preview.source_completion_status || "未注明");
        const options = preview.review_options || {};
        const addReviewSelect = (key, title, values, current) => {
          const label = document.createElement("label");
          label.className = "remote-review-field";
          label.textContent = title;
          const select = document.createElement("select");
          select.dataset.remoteReviewField = key;
          if (!current) {
            const pending = document.createElement("option");
            pending.value = "";
            pending.textContent = "请核对后选择";
            select.append(pending);
          }
          values.forEach((value) => {
            const option = document.createElement("option");
            option.value = typeof value === "string" ? value : value.label;
            option.textContent = option.value;
            option.selected = option.value === current;
            select.append(option);
          });
          select.addEventListener("change", () => {
            if (confirm) confirm.disabled = true;
            setMessage(dialogMessage, "类别或状态已修改，请先更新确认内容。");
          });
          reviewFields[key] = select;
          label.append(select);
          controls.append(label);
        };
        addReviewSelect("service_category", "服务内容类别", options.categories || [], preview.service_category);
        addReviewSelect("completion_status", "CRM 处理状态", options.completion_statuses || [], preview.completion_status);
        if (!preview.completion_status) {
          const notice = document.createElement("p");
          notice.className = "notice";
          notice.textContent = "日报未确认完成；请按实际进展选择 CRM 处理状态，再更新确认内容。";
          controls.append(notice);
        }
      }

      if (preview.requires_hours || preview.action === "remote_workorder") {
        const label = document.createElement("label");
        label.textContent = "本次工时（小时）";
        const input = document.createElement("input");
        input.type = "number";
        input.min = "0.5";
        input.step = "0.5";
        input.inputMode = "decimal";
        input.dataset.hoursInput = "";
        input.value = preview.hours || "";
        input.addEventListener("input", () => { if (confirm) confirm.disabled = true; });
        const rebuild = document.createElement("button");
        rebuild.type = "button";
        rebuild.className = "command-button";
        rebuild.textContent = "更新确认内容";
        rebuild.addEventListener("click", async () => {
          rebuild.disabled = true;
          try {
            const payload = await postForm(`/days/${reportDate}/preview`, token, {
              action: preview.action,
              customer: preview.customer,
              hours: input.value,
              includes_main_work: preview.includes_main_work,
              ...Object.fromEntries(Object.entries(reviewFields).map(([key, field]) => [key, field.value])),
            });
            setMessage(dialogMessage, "");
            renderPreviews(payload.previews.filter((entry) => entry.customer === preview.customer
              && entry.includes_main_work === preview.includes_main_work));
          } catch (error) {
            setMessage(dialogMessage, error.message, true);
          } finally {
            rebuild.disabled = false;
          }
        });
        label.append(input);
        controls.append(label, rebuild);
      }
      if (!preview.requires_hours) {
        confirm = document.createElement("button");
        confirm.type = "button";
        confirm.className = "command-button";
        confirm.textContent = "确认并排队";
        confirm.disabled = preview.action === "remote_workorder" && !preview.completion_status;
        confirm.addEventListener("click", async () => {
          confirm.disabled = true;
          try {
            const payload = await postForm("/actions/authorize", token, {
              report_date: preview.report_date,
              action: preview.action,
              customer: preview.customer,
              hours: preview.hours || "",
              authorization_digest: preview.authorization_digest,
              ...(preview.action === "remote_workorder" ? {
                includes_main_work: preview.includes_main_work,
                service_category: preview.service_category,
                completion_status: preview.completion_status,
              } : {}),
            });
            window.location.assign(payload.job_url);
          } catch (error) {
            setMessage(dialogMessage, error.message, true);
            confirm.disabled = false;
          }
        });
        controls.append(confirm);
      }
      item.append(facts, content, controls);
      previewList.append(item);
    });
  };

  panel.querySelectorAll("[data-preview-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      setMessage(panelMessage, "正在生成预览…");
      try {
        const payload = await postForm(`/days/${reportDate}/preview`, token, {
          action: button.dataset.previewAction,
        });
        setMessage(dialogMessage, "");
        renderPreviews(payload.previews);
        dialog.showModal();
        setMessage(panelMessage, "");
      } catch (error) {
        setMessage(panelMessage, error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  });

  const sourceSyncButton = panel.querySelector("[data-source-sync-action]");
  const sourceSyncDialog = document.querySelector("[data-source-sync-dialog]");
  if (sourceSyncButton && sourceSyncDialog) {
    const sourceSyncList = sourceSyncDialog.querySelector("[data-source-sync-list]");
    const sourceSyncMessage = sourceSyncDialog.querySelector("[data-source-sync-message]");
    const sourceSyncConfirm = sourceSyncDialog.querySelector("[data-confirm-source-sync]");
    const sourceSyncTotal = sourceSyncDialog.querySelector("[data-source-sync-total]");
    const sourceSyncUrl = `/days/${reportDate}/source-sync`;
    let sourceSyncState = [];
    let authorizingSourceSync = false;

    const setSourceSyncMessage = (message, isError = false) => {
      sourceSyncMessage.textContent = message;
      sourceSyncMessage.classList.toggle("is-error", isError);
    };

    const updateSourceSyncConfirm = () => {
      const selectedCount = sourceSyncState.filter((entry) => entry.selected).length;
      sourceSyncConfirm.disabled = authorizingSourceSync || selectedCount === 0;
      sourceSyncConfirm.textContent = selectedCount
        ? `确认并排队（${selectedCount} 项）`
        : "确认并排队";
      if (sourceSyncTotal) {
        const remote = sourceSyncState.filter((entry) => entry.selected && entry.preview.action === "remote_workorder");
        const valid = remote.filter((entry) => Number.isFinite(Number(entry.hours)) && Number(entry.hours) > 0);
        const total = valid.reduce((sum, entry) => sum + Number(entry.hours), 0);
        sourceSyncTotal.textContent = `已选远程工单 ${remote.length} 张，合计 ${total} 小时${valid.length < remote.length ? "（仍有工时待填写）" : ""}。工时按工单填写，合计不重复分配。`;
      }
    };

    const renderSourceSync = () => {
      sourceSyncList.replaceChildren();
      sourceSyncState.forEach((state) => {
        const { preview } = state;
        const item = document.createElement("article");
        item.className = "preview-item source-sync-item";

        const choice = document.createElement("label");
        choice.className = "source-sync-choice";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = state.selected;
        checkbox.disabled = preview.required || Boolean(preview.selection_blocked);
        const choiceText = document.createElement("span");
        choiceText.textContent = preview.required
          ? (preview.action === "daily_log" ? "必选：日报" : "必选：主服务")
          : (preview.includes_main_work ? "可选：历史主服务" : "可选：额外远程支持");
        choice.append(checkbox, choiceText);

        const facts = document.createElement("div");
        facts.className = "preview-facts";
        appendFact(facts, "操作", preview.action_label);
        appendFact(facts, "日期", preview.report_date);
        appendFact(facts, "客户", preview.customer);
        if (preview.action === "onsite_service" && preview.work_type) {
          appendFact(facts, "工作类型", preview.work_type);
          if (preview.routing_rule) appendFact(facts, "关联规则", preview.routing_rule);
          if (preview.routing_target) {
            appendFact(facts, "关联工单", preview.routing_target.workorder_id);
            appendFact(facts, "工单创建时间", preview.routing_target.created_at);
          }
        }
        const hoursFact = appendFact(
          facts,
          "工时",
          state.hours ? `${state.hours} 小时` : (preview.requires_hours ? "待填写" : "不适用")
        );

        const content = document.createElement("pre");
        content.className = "preview-content";
        content.textContent = preview.content;
        item.append(choice, facts, content);
        if (preview.selection_blocked) {
          const reason = document.createElement("p");
          reason.className = "notice";
          reason.textContent = preview.selection_blocked;
          item.append(reason);
        }

        if (preview.action === "remote_workorder") {
          appendFact(facts, "日报处理结果", preview.source_completion_status || "未注明");
          [["service_category", "服务内容类别", preview.review_options.categories],
            ["completion_status", "CRM 处理状态", preview.review_options.completion_statuses]].forEach(([key, title, values]) => {
            const label = document.createElement("label");
            label.className = "remote-review-field";
            label.textContent = title;
            const select = document.createElement("select");
            const choices = state[key] ? values : ["", ...values];
            choices.forEach((value) => {
              const option = document.createElement("option");
              option.value = typeof value === "string" ? value : value.label;
              option.textContent = option.value || "请核对后选择";
              option.selected = option.value === state[key];
              select.append(option);
            });
            select.addEventListener("change", () => { state[key] = select.value; });
            label.append(select);
            item.append(label);
          });
        }

        if (preview.action === "remote_workorder") {
          const hoursLabel = document.createElement("label");
          hoursLabel.className = "source-sync-hours";
          hoursLabel.textContent = "本次工时（0.5–8 小时）";
          const input = document.createElement("input");
          input.type = "number";
          input.min = "0.5";
          input.max = "8";
          input.step = "0.5";
          input.inputMode = "decimal";
          input.value = state.hours;
          input.disabled = !state.selected;
          input.addEventListener("input", () => {
            state.hours = input.value;
            hoursFact.textContent = state.hours ? `${state.hours} 小时` : "待填写";
            updateSourceSyncConfirm();
          });
          hoursLabel.append(input);
          item.append(hoursLabel);
          if (!checkbox.disabled) checkbox.addEventListener("change", () => {
            if (checkbox.checked && preview.selection_key && sourceSyncState.some((entry) =>
              entry !== state && entry.selected && entry.preview.selection_key === preview.selection_key
            )) {
              checkbox.checked = false;
              setSourceSyncMessage(`“${preview.customer}”同日只能选择一张远程工单，内容不会合并。`, true);
              return;
            }
            state.selected = checkbox.checked;
            input.disabled = !state.selected;
            updateSourceSyncConfirm();
          });
        }
        sourceSyncList.append(item);
      });
      updateSourceSyncConfirm();
    };

    const selectedSourceSyncEntries = () => {
      const selected = sourceSyncState.filter((entry) => entry.selected);
      if (!selected.length) throw new Error("请至少选择一项后再确认。");
      return selected.map((state) => {
        if (state.preview.action === "remote_workorder" && !state.completion_status) {
          throw new Error(`“${state.preview.customer}”尚未确认 CRM 处理状态，请按实际进展选择后再提交。`);
        }
        const authorization_digest = state.preview.authorization_digest;
        if (state.preview.action !== "remote_workorder") return { authorization_digest };
        const value = Number(state.hours);
        if (
          !Number.isFinite(value)
          || value < 0.5
          || value > 8
          || !Number.isInteger(value * 2)
        ) {
          throw new Error(`请为“${state.preview.customer}”填写 0.5 到 8 之间、且为 0.5 倍数的工时。`);
        }
        return {
          authorization_digest,
          hours: String(value),
          service_category: state.service_category,
          completion_status: state.completion_status,
        };
      });
    };

    sourceSyncButton.addEventListener("click", async () => {
      sourceSyncButton.disabled = true;
      setMessage(panelMessage, "正在从日报.md 读取并生成同步预检…");
      try {
        const payload = await postForm(`${sourceSyncUrl}/preview`, token, {});
        sourceSyncState = payload.previews.map((preview) => ({
          preview,
          selected: preview.required,
          hours: preview.hours || "",
          service_category: preview.service_category || "",
          completion_status: preview.completion_status || "",
        }));
        if (!sourceSyncState.length) throw new Error("该日报没有可同步的内容。");
        setSourceSyncMessage(payload.warning || "");
        renderSourceSync();
        sourceSyncDialog.showModal();
        setMessage(panelMessage, "");
      } catch (error) {
        setMessage(panelMessage, error.message, true);
      } finally {
        sourceSyncButton.disabled = false;
      }
    });

    sourceSyncConfirm.addEventListener("click", async () => {
      if (authorizingSourceSync) return;
      let selected;
      try {
        selected = selectedSourceSyncEntries();
      } catch (error) {
        setSourceSyncMessage(error.message, true);
        return;
      }
      authorizingSourceSync = true;
      updateSourceSyncConfirm();
      setSourceSyncMessage("正在重新核对日报源文件…");
      try {
        const payload = await postForm(`${sourceSyncUrl}/authorize`, token, {
          selections: JSON.stringify(selected),
        });
        setMessage(panelMessage, `已将 ${payload.job_ids.length} 项加入任务队列，正在跳转任务页。`);
        window.location.assign(payload.job_url);
      } catch (error) {
        setSourceSyncMessage(error.message, true);
        authorizingSourceSync = false;
        updateSourceSyncConfirm();
      }
    });

    if (panel.dataset.autoSourceSync === "true") {
      window.setTimeout(() => sourceSyncButton.click(), 0);
    }
  }

  panel.querySelectorAll("[data-verify-step]").forEach((button) => {
    button.addEventListener("click", async () => {
      const verificationResult = panel.querySelector("[data-inline-verification-result]");
      const verificationButtons = Array.from(panel.querySelectorAll("[data-verify-step]"));
      const autoDeleteVerificationStatuses = new Set(["not_found"]);
      const diagnosticVerificationStatuses = new Set([
        "unknown",
        "pending_retry",
        "uncertain",
      ]);
      const renderVerificationResult = (message, isError = false, taskUrl = "") => {
        if (!verificationResult) return;
        verificationResult.replaceChildren(document.createTextNode(message));
        if (taskUrl) {
          const taskLink = document.createElement("a");
          taskLink.href = taskUrl;
          taskLink.className = "inline-verification-result__link";
          taskLink.textContent = "查看任务";
          verificationResult.append(document.createTextNode(" "), taskLink);
        }
        verificationResult.classList.toggle("is-error", isError);
      };
      const verificationDetail = (job) => {
        const detail = String(
          job.error_summary || job.result_payload?.message || ""
        ).replace(/\s+/g, " ").trim();
        return detail.length > 180 ? `${detail.slice(0, 180)}…` : detail;
      };
      const finishVerification = async (job) => {
        const statusMessages = {
          crm_verified: "CRM 校验成功：已确认记录存在，正在刷新状态。",
          not_found: "CRM 校验未找到对应记录；失败结果不会写入每日提交状态。",
          unknown: "CRM 校验未完成：请查看任务结果后再决定下一步。",
          pending_retry: "CRM 校验执行失败：请查看任务结果后重试。",
          uncertain: "CRM 校验结果待核实：请查看任务结果。",
        };
        let message = statusMessages[job.status] || `CRM 校验完成：${job.status_label || job.status}`;
        let taskUrl = "";
        if (autoDeleteVerificationStatuses.has(job.status)) {
          try {
            await postForm(
              `/days/${reportDate}/crm-verifications/${job.job_id}/delete`,
              token,
              {}
            );
            message = `${message} 失败校验记录已自动清理。`;
          } catch (error) {
            message = `${message} 自动清理失败：${error.message}`;
          }
        }
        if (diagnosticVerificationStatuses.has(job.status)) {
          const detail = verificationDetail(job);
          if (detail) message = `${message} 原因：${detail}`;
          message = `${message} 已保留诊断任务。`;
          taskUrl = `/jobs?focus=${encodeURIComponent(job.job_id)}`;
        }
        renderVerificationResult(message, job.status !== "crm_verified", taskUrl);
        verificationButtons.forEach((entry) => { entry.disabled = false; });
        if (job.status === "crm_verified") {
          window.setTimeout(() => window.location.reload(), 1200);
        }
      };
      const pollInlineVerification = async (pollUrl) => {
        try {
          const response = await fetch(pollUrl);
          if (!response.ok) throw new Error(`CRM 校验任务查询失败（${response.status}）`);
          const job = await response.json();
          if (job.poll || ["awaiting_confirmation", "queued", "running"].includes(job.status)) {
            renderVerificationResult(`CRM 校验中：${job.status_label || "正在查询纷享销客"}`);
            window.setTimeout(() => pollInlineVerification(pollUrl), 1000);
            return;
          }
          await finishVerification(job);
        } catch (error) {
          renderVerificationResult(error.message, true);
          verificationButtons.forEach((entry) => { entry.disabled = false; });
        }
      };
      verificationButtons.forEach((entry) => { entry.disabled = true; });
      setMessage(panelMessage, "");
      renderVerificationResult("正在创建 CRM 只读校验任务…");
      try {
        const target = panel.querySelector(`[data-verification-target="${button.dataset.verifyStep}"]`);
        if (target && !target.value) throw new Error("请先选择要复核的客户和工单编号。");
        const payload = await postForm(`/days/${reportDate}/verify`, token, {
          step: button.dataset.verifyStep,
          unique_key: target?.value || "",
        });
        pollInlineVerification(payload.poll_url);
      } catch (error) {
        renderVerificationResult(error.message, true);
        verificationButtons.forEach((entry) => { entry.disabled = false; });
      }
    });
  });

  const manualDialog = document.querySelector("[data-manual-override-dialog]");
  if (manualDialog) {
    const manualTitle = manualDialog.querySelector("[data-manual-override-title]");
    const manualFacts = manualDialog.querySelector("[data-manual-override-facts]");
    const manualBoundary = manualDialog.querySelector("[data-manual-override-boundary]");
    const manualMessage = manualDialog.querySelector("[data-manual-override-message]");
    const manualConfirm = manualDialog.querySelector("[data-confirm-manual-override]");
    let manualState = null;

    document.querySelectorAll("[data-manual-override-step]").forEach((button) => {
      button.addEventListener("click", () => {
        manualState = {
          step: button.dataset.manualOverrideStep,
          label: button.dataset.manualOverrideLabel,
          mode: button.dataset.manualOverrideMode,
        };
        const isRevoke = manualState.mode === "revoke";
        manualTitle.textContent = isRevoke ? "撤销人工更正" : "确认人工更正";
        manualFacts.replaceChildren();
        appendFact(manualFacts, "日期", reportDate);
        appendFact(manualFacts, "类型", manualState.label);
        appendFact(
          manualFacts,
          "显示结果",
          isRevoke ? "恢复自动化记录显示" : "人工确认已提交，未 CRM 核验"
        );
        manualBoundary.textContent = isRevoke
          ? "仅撤销本机 Web 的人工显示更正，不会修改待补交队列或提交历史。"
          : "仅更正本机 Web 显示，不会修改待补交队列、提交历史或 CRM 数据。";
        manualMessage.textContent = "";
        manualMessage.classList.remove("is-error");
        manualConfirm.textContent = isRevoke ? "确认撤销" : "确认人工更正";
        manualDialog.showModal();
      });
    });

    manualConfirm.addEventListener("click", async () => {
      if (!manualState) return;
      manualConfirm.disabled = true;
      try {
        const suffix = manualState.mode === "revoke" ? "/revoke" : "";
        await postForm(`/days/${reportDate}/manual-override${suffix}`, token, {
          step: manualState.step,
        });
        window.location.reload();
      } catch (error) {
        manualMessage.textContent = error.message;
        manualMessage.classList.add("is-error");
        manualConfirm.disabled = false;
      }
    });
  }

  const deleteVerificationDialog = document.querySelector("[data-delete-crm-verification-dialog]");
  if (deleteVerificationDialog) {
    const deleteFacts = deleteVerificationDialog.querySelector("[data-delete-crm-verification-facts]");
    const deleteMessage = deleteVerificationDialog.querySelector("[data-delete-crm-verification-message]");
    const deleteConfirm = deleteVerificationDialog.querySelector("[data-confirm-delete-crm-verification]");
    let verificationJobId = "";

    document.querySelectorAll("[data-delete-crm-verification]").forEach((button) => {
      button.addEventListener("click", () => {
        verificationJobId = button.dataset.jobId || "";
        deleteFacts.replaceChildren();
        appendFact(deleteFacts, "日期", reportDate);
        appendFact(deleteFacts, "类型", button.dataset.stepLabel || "CRM 校验");
        appendFact(deleteFacts, "校验任务", verificationJobId);
        deleteMessage.textContent = "";
        deleteMessage.classList.remove("is-error");
        deleteVerificationDialog.showModal();
      });
    });

    deleteConfirm.addEventListener("click", async () => {
      if (!verificationJobId) return;
      deleteConfirm.disabled = true;
      try {
        await postForm(
          `/days/${reportDate}/crm-verifications/${encodeURIComponent(verificationJobId)}/delete`,
          token,
          {},
        );
        window.location.reload();
      } catch (error) {
        deleteMessage.textContent = error.message;
        deleteMessage.classList.add("is-error");
        deleteConfirm.disabled = false;
      }
    });
  }

  const remoteHoursDialog = document.querySelector("[data-remote-hours-dialog]");
  if (!remoteHoursDialog) return;
  const remoteHoursFacts = remoteHoursDialog.querySelector("[data-remote-hours-facts]");
  const remoteHoursInput = remoteHoursDialog.querySelector("[data-remote-hours-input]");
  const remoteHoursMessage = remoteHoursDialog.querySelector("[data-remote-hours-message]");
  const remoteHoursConfirm = remoteHoursDialog.querySelector("[data-confirm-remote-hours]");
  const remoteBindingDialog = document.querySelector("[data-remote-binding-dialog]");
  const remoteBindingFacts = remoteBindingDialog?.querySelector("[data-remote-binding-facts]");
  const remoteBindingInput = remoteBindingDialog?.querySelector("[data-remote-binding-input]");
  const remoteBindingMessage = remoteBindingDialog?.querySelector("[data-remote-binding-message]");
  const remoteBindingConfirm = remoteBindingDialog?.querySelector("[data-confirm-remote-binding]");
  let remoteHoursState = null;
  let remoteBindingState = null;

  const setRemoteHourStatus = (row, message, isError = false) => {
    const status = row.querySelector("[data-remote-hours-status]");
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("is-error", isError);
  };

  const setRemoteHourValue = (row, hours) => {
    if (hours === null || hours === undefined || hours === "") return;
    const normalized = String(Number(hours));
    row.dataset.currentHours = normalized;
    const value = row.querySelector("[data-remote-hours-value]");
    if (value) value.textContent = `${normalized} 小时`;
    const editor = row.querySelector("[data-remote-hours-edit]");
    if (editor) {
      editor.dataset.currentHours = normalized;
      editor.dataset.baseHours = normalized;
    }
  };

  const renderRemoteHourJob = (row, job) => {
    const requestPayload = job.request_payload || {};
    const resultPayload = job.result_payload || {};
    const currentHours = row.dataset.currentHours || "—";
    const targetHours = requestPayload.target_hours;
    const editor = row.querySelector("[data-remote-hours-edit]");
    row.dataset.remoteHoursJob = job.job_id;
    row.dataset.remoteHoursJobStatus = job.status;

    if (job.poll || ["queued", "running"].includes(job.status)) {
      if (editor) editor.disabled = true;
      const prefix = targetHours ? `${currentHours} → ${targetHours} 小时 · ` : "";
      setRemoteHourStatus(
        row,
        `${prefix}${job.action === "remote_workorder_binding_verification" ? "CRM 只读核验中" : "CRM 同步中"}`
      );
      return true;
    }

    if (job.status === "crm_verified") {
      setRemoteHourValue(row, resultPayload.crm_observed_hours);
      if (resultPayload.workorder_id) {
        row.dataset.workorderId = resultPayload.workorder_id;
        const workorderCell = row.querySelector("[data-remote-hours-workorder]");
        if (workorderCell) workorderCell.textContent = resultPayload.workorder_id;
        if (editor) editor.dataset.workorderId = resultPayload.workorder_id;
      }
      if (resultPayload.crm_customer) {
        row.dataset.customer = resultPayload.crm_customer;
        const customerCell = row.querySelector("[data-remote-hours-customer]");
        if (customerCell) customerCell.textContent = resultPayload.crm_customer;
        if (editor) editor.dataset.customer = resultPayload.crm_customer;
      }
      if (editor) editor.disabled = false;
      setRemoteHourStatus(row, "CRM 已确认", false);
      return false;
    }

    if (job.status === "pending_retry") {
      if (editor) editor.disabled = false;
      setRemoteHourStatus(row, "提交前同步失败", true);
      return false;
    }

    if (job.status === "conflict") {
      if (resultPayload.conflict_kind === "hours") {
        setRemoteHourValue(row, resultPayload.crm_observed_hours);
        if (editor) editor.disabled = false;
      } else {
        if (editor) editor.disabled = true;
        if (!row.querySelector("[data-repair-workorder-binding]")) {
          window.setTimeout(() => window.location.reload(), 400);
        }
      }
      setRemoteHourStatus(row, resultPayload.message || "CRM 工单校验冲突", true);
      return false;
    }

    if (job.status === "uncertain") {
      if (editor) editor.disabled = true;
      setRemoteHourStatus(row, "CRM 结果待核实，不会自动重提", true);
      return false;
    }

    if (job.status === "not_found") {
      if (editor) editor.disabled = true;
      setRemoteHourStatus(row, "CRM 未找到对应远程工单", true);
      if (!row.querySelector("[data-repair-workorder-binding]")) {
        window.setTimeout(() => window.location.reload(), 400);
      }
      return false;
    }

    setRemoteHourStatus(row, job.status_label || job.status, true);
    return false;
  };

  const pollRemoteHourJob = (row, pollUrl) => {
    if (!row || !pollUrl || row.dataset.remoteHoursPolling === pollUrl) return;
    row.dataset.remoteHoursPolling = pollUrl;
    const isCurrent = () => row.dataset.remoteHoursPolling === pollUrl;
    const poll = async () => {
      if (!isCurrent()) return;
      try {
        const response = await fetch(pollUrl);
        if (!response.ok) throw new Error(`任务轮询失败（${response.status}）`);
        const job = await response.json();
        if (!isCurrent()) return;
        const keepPolling = renderRemoteHourJob(row, job);
        if (keepPolling) {
          window.setTimeout(poll, 1000);
        } else {
          delete row.dataset.remoteHoursPolling;
        }
      } catch (_) {
        if (isCurrent()) window.setTimeout(poll, 2000);
      }
    };
    window.setTimeout(poll, 500);
  };

  document.querySelectorAll("[data-remote-hours-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      remoteHoursState = {
        row: button.closest("[data-remote-hours-row]"),
        contentDigest: button.dataset.contentDigest,
        workorderId: button.dataset.workorderId,
        customer: button.dataset.customer,
        baseHours: button.dataset.baseHours,
      };
      remoteHoursFacts.replaceChildren();
      appendFact(remoteHoursFacts, "日期", reportDate);
      appendFact(remoteHoursFacts, "客户", remoteHoursState.customer);
      appendFact(remoteHoursFacts, "CRM 工单", remoteHoursState.workorderId || "未获取编号");
      appendFact(remoteHoursFacts, "CRM 已确认工时", button.dataset.currentHours ? `${button.dataset.currentHours} 小时` : "未记录");
      remoteHoursInput.value = button.dataset.currentHours || "0.5";
      remoteHoursMessage.textContent = "";
      remoteHoursMessage.classList.remove("is-error");
      remoteHoursConfirm.disabled = false;
      remoteHoursDialog.showModal();
    });
  });

  remoteHoursConfirm.addEventListener("click", async () => {
    if (!remoteHoursState) return;
    const requestState = remoteHoursState;
    remoteHoursConfirm.disabled = true;
    try {
      const payload = await postForm(`/days/${reportDate}/remote-hours`, token, {
        content_digest: requestState.contentDigest,
        workorder_id: requestState.workorderId,
        service_hours: remoteHoursInput.value,
      });
      if (remoteHoursState === requestState) remoteHoursDialog.close();
      if (requestState.row) {
        requestState.row.dataset.remoteHoursJob = payload.job_id;
        requestState.row.dataset.remoteHoursJobApi = payload.poll_url;
        renderRemoteHourJob(requestState.row, {
          job_id: payload.job_id,
          action: "remote_workorder_hours_sync",
          status: "queued",
          request_payload: { target_hours: payload.target_hours },
          result_payload: {},
        });
        pollRemoteHourJob(requestState.row, payload.poll_url);
      }
    } catch (error) {
      if (remoteHoursState === requestState) {
        remoteHoursMessage.textContent = error.message;
        remoteHoursMessage.classList.add("is-error");
        remoteHoursConfirm.disabled = false;
      } else if (requestState.row) {
        setRemoteHourStatus(requestState.row, error.message, true);
      }
    }
  });

  document.querySelectorAll("[data-confirm-customer-alias]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await postForm(`/jobs/${button.dataset.jobId}/confirm-customer-alias`, token, {});
        window.location.reload();
      } catch (error) {
        const row = button.closest("[data-remote-hours-row]");
        if (row) setRemoteHourStatus(row, error.message, true);
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-repair-workorder-binding]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!remoteBindingDialog || !remoteBindingFacts || !remoteBindingInput || !remoteBindingMessage) return;
      remoteBindingState = {
        row: button.closest("[data-remote-hours-row]"),
        contentDigest: button.dataset.contentDigest,
        workorderId: button.dataset.workorderId,
        customer: button.dataset.customer,
      };
      remoteBindingFacts.replaceChildren();
      appendFact(remoteBindingFacts, "日期", reportDate);
      appendFact(remoteBindingFacts, "客户", remoteBindingState.customer);
      appendFact(remoteBindingFacts, "当前关联", remoteBindingState.workorderId);
      remoteBindingInput.value = "";
      remoteBindingMessage.textContent = "";
      remoteBindingMessage.classList.remove("is-error");
      remoteBindingConfirm.disabled = false;
      remoteBindingDialog.showModal();
      remoteBindingInput.focus();
    });
  });

  remoteBindingConfirm?.addEventListener("click", async () => {
    if (!remoteBindingState || !remoteBindingInput) return;
    const requestState = remoteBindingState;
    remoteBindingConfirm.disabled = true;
    try {
      const payload = await postForm(`/days/${reportDate}/remote-workorder-binding`, token, {
        content_digest: requestState.contentDigest,
        replacement_workorder_id: remoteBindingInput.value,
      });
      if (remoteBindingState === requestState) remoteBindingDialog.close();
      if (requestState.row) {
        renderRemoteHourJob(requestState.row, {
          job_id: payload.job_id,
          action: "remote_workorder_binding_verification",
          status: "queued",
          request_payload: {},
          result_payload: {},
        });
        pollRemoteHourJob(requestState.row, payload.poll_url);
      }
    } catch (error) {
      if (remoteBindingState === requestState) {
        remoteBindingMessage.textContent = error.message;
        remoteBindingMessage.classList.add("is-error");
        remoteBindingConfirm.disabled = false;
      } else if (requestState.row) {
        setRemoteHourStatus(requestState.row, error.message, true);
      }
    }
  });

  document.querySelectorAll("[data-remote-hours-row][data-remote-hours-job]").forEach((row) => {
    if (["queued", "running", "crm_id_captured"].includes(row.dataset.remoteHoursJobStatus)) {
      pollRemoteHourJob(row, row.dataset.remoteHoursJobApi);
    }
  });
};

const initializeJobCenter = () => {
  const center = document.querySelector("[data-job-center]");
  if (!center) return;
  const token = center.dataset.localToken;
  const focus = center.dataset.focusJob;
  if (focus) document.querySelector(`#job-${CSS.escape(focus)}`)?.classList.add("is-focused");

  const updateRow = (row, job) => {
    row.dataset.jobStatus = job.status;
    const status = row.querySelector("[data-job-status-label]");
    status.textContent = job.status_label;
    status.className = `status-code status-code--${job.status}`;
    const error = row.querySelector("[data-job-error]");
    error.textContent = job.error_summary || "—";
    const progress = row.querySelector("[data-job-progress]");
    if (progress) progress.textContent = job.progress_log.join("\n");
    const deleteButton = row.querySelector("[data-delete-job]");
    if (deleteButton) {
      deleteButton.hidden = ![
        "queued",
        "pending_retry",
        "crm_verified",
        "skipped",
        "not_found",
        "unknown",
      ].includes(job.status);
    }
    return job.poll;
  };

  center.querySelectorAll("[data-job-row]").forEach((row) => {
    if (row.dataset.jobPolling !== "true") return;
    const poll = async () => {
      try {
        const response = await fetch(row.dataset.jobApi);
        if (response.status === 404) {
          window.location.reload();
          return;
        }
        if (!response.ok) throw new Error(`任务查询失败（${response.status}）`);
        const keepPolling = updateRow(row, await response.json());
        if (keepPolling) window.setTimeout(poll, 1000);
        else window.setTimeout(() => window.location.reload(), 350);
      } catch (_) {
        window.setTimeout(poll, 2000);
      }
    };
    window.setTimeout(poll, 500);
  });

  const deleteDialog = document.querySelector("[data-delete-job-dialog]");
  if (deleteDialog) {
    const deleteFacts = deleteDialog.querySelector("[data-delete-job-facts]");
    const deleteMessage = deleteDialog.querySelector("[data-delete-job-message]");
    const deleteConfirm = deleteDialog.querySelector("[data-confirm-delete-job]");
    let deleteState = null;

    center.querySelectorAll("[data-delete-job]").forEach((button) => {
      button.addEventListener("click", () => {
        const row = button.closest("[data-job-row]");
        deleteState = {
          jobId: row.dataset.jobId,
          row,
        };
        deleteFacts.replaceChildren();
        appendFact(deleteFacts, "任务编号", row.dataset.jobId);
        appendFact(deleteFacts, "日期", row.dataset.reportDate);
        appendFact(deleteFacts, "客户", row.dataset.customer);
        appendFact(deleteFacts, "当前状态", row.dataset.jobStatus);
        deleteMessage.textContent = "";
        deleteMessage.classList.remove("is-error");
        deleteConfirm.disabled = false;
        deleteDialog.showModal();
      });
    });

    deleteConfirm.addEventListener("click", async () => {
      if (!deleteState) return;
      deleteConfirm.disabled = true;
      try {
        await postForm(`/jobs/${deleteState.jobId}/delete`, token, {});
        window.location.reload();
      } catch (error) {
        deleteMessage.textContent = error.message;
        deleteMessage.classList.add("is-error");
        deleteConfirm.disabled = false;
      }
    });
  }

  const dialog = document.querySelector("[data-retry-dialog]");
  if (!dialog) return;
  const previewHost = dialog.querySelector("[data-retry-preview]");
  const message = dialog.querySelector("[data-retry-message]");
  const confirm = dialog.querySelector("[data-confirm-retry]");
  let retryState = null;

  center.querySelectorAll("[data-retry-mode]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-job-row]");
      message.textContent = "";
      button.disabled = true;
      try {
        const reviewValues = row.dataset.jobAction === "remote_workorder"
          ? Object.fromEntries([
            ["service_category", row.dataset.serviceCategory],
            ["completion_status", row.dataset.completionStatus],
          ].filter(([, value]) => value))
          : {};
        const payload = await postForm(`/days/${row.dataset.reportDate}/preview`, token, {
          action: row.dataset.jobAction,
          customer: row.dataset.customer,
          hours: row.dataset.hours,
          ...reviewValues,
        });
        const preview = payload.previews.find((entry) =>
          entry.customer === row.dataset.customer && entry.content_digest === row.dataset.contentDigest
        );
        if (!preview || preview.requires_hours) throw new Error("无法重建原任务预览，请从日期详情重新操作。");
        retryState = { row, preview, mode: button.dataset.retryMode };
        previewHost.replaceChildren();
        const facts = document.createElement("div");
        facts.className = "preview-facts";
        appendFact(facts, "日期", preview.report_date);
        appendFact(facts, "客户", preview.customer);
        appendFact(facts, "工时", preview.hours ? `${preview.hours} 小时` : "—");
        if (preview.action === "remote_workorder") {
          appendFact(facts, "服务内容类别", preview.service_category);
          appendFact(facts, "CRM 处理状态", preview.completion_status);
        }
        appendFact(facts, "模式", retryState.mode === "visible" ? "显示浏览器" : "后台");
        const content = document.createElement("pre");
        content.className = "preview-content";
        content.textContent = preview.content;
        previewHost.append(facts, content);
        dialog.showModal();
      } catch (error) {
        row.querySelector("[data-job-error]").textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
  });

  confirm.addEventListener("click", async () => {
    if (!retryState) return;
    confirm.disabled = true;
    try {
      const payload = await postForm(`/jobs/${retryState.row.dataset.jobId}/retry`, token, {
        authorization_digest: retryState.preview.authorization_digest,
        browser_mode: retryState.mode,
      });
      window.location.assign(payload.job_url);
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("is-error");
      confirm.disabled = false;
    }
  });
};

const initializeArtifactCleanup = () => {
  const center = document.querySelector("[data-artifact-center]");
  const previewButton = document.querySelector("[data-cleanup-preview]");
  const dialog = document.querySelector("[data-cleanup-dialog]");
  if (!center || !previewButton || !dialog) return;
  const token = center.dataset.localToken;
  const message = center.querySelector("[data-cleanup-message]");
  const dialogMessage = dialog.querySelector("[data-cleanup-dialog-message]");
  const summary = dialog.querySelector("[data-cleanup-summary]");
  const list = dialog.querySelector("[data-cleanup-list]");
  const confirm = dialog.querySelector("[data-confirm-cleanup]");
  let previewDigest = "";

  previewButton.addEventListener("click", async () => {
    previewButton.disabled = true;
    message.textContent = "正在计算清理候选…";
    try {
      const payload = await postForm("/artifacts/cleanup/preview", token, {});
      previewDigest = payload.preview_digest;
      summary.textContent = `${payload.candidate_count} 个文件，共 ${payload.total_size}`;
      list.replaceChildren();
      payload.candidates.forEach((candidate) => {
        const item = document.createElement("li");
        const name = document.createElement("strong");
        const size = document.createElement("span");
        name.textContent = candidate.file_name;
        size.textContent = candidate.size;
        item.append(name, size);
        list.append(item);
      });
      confirm.disabled = payload.candidate_count === 0;
      if (!payload.candidate_count) {
        dialogMessage.textContent = "";
        message.textContent = "没有可清理的留痕：文件仍在保留期内或正被待补交任务保护。";
        return;
      }
      dialogMessage.textContent = "请核对候选文件后确认删除。";
      message.textContent = "";
      dialog.showModal();
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("is-error");
    } finally {
      previewButton.disabled = false;
    }
  });

  confirm.addEventListener("click", async () => {
    if (!previewDigest) return;
    confirm.disabled = true;
    try {
      const payload = await postForm("/artifacts/cleanup/confirm", token, {
        preview_digest: previewDigest,
      });
      dialog.close();
      message.textContent = `已删除 ${payload.deleted_count} 个文件，共 ${payload.deleted_size}`;
      window.location.reload();
    } catch (error) {
      dialogMessage.textContent = error.message;
      dialogMessage.classList.add("is-error");
      confirm.disabled = false;
    }
  });
};

const initializeArtifactFilter = () => {
  const filter = document.querySelector("[data-artifact-filter]");
  if (!filter) return;
  filter.querySelectorAll("select").forEach((select) => {
    select.addEventListener("change", () => filter.requestSubmit());
  });
};

const initializeKnowledgeBaseSync = () => {
  const center = document.querySelector("[data-knowledge-base-center]");
  const buttons = Array.from(document.querySelectorAll("[data-knowledge-base-sync-button]"));
  if (!center || buttons.length === 0) return;

  const token = center.dataset.localToken;
  const message = center.querySelector("[data-knowledge-base-sync-message]");
  const status = center.querySelector("[data-knowledge-base-status]");
  const statusDot = center.querySelector(".status-dot");
  const fullSyncDialog = document.querySelector("[data-knowledge-base-full-sync-dialog]");
  const confirmFullSync = document.querySelector("[data-confirm-knowledge-base-full-sync]");
  let polling = false;

  const setButtonsDisabled = (disabled) => {
    buttons.forEach((button) => { button.disabled = disabled; });
  };

  const setState = (state, label, detail, isError = false) => {
    status.textContent = label;
    statusDot.className = `status-dot status-${state}`;
    message.textContent = detail || "";
    message.classList.toggle("is-error", isError);
  };

  const completePolling = (payload) => {
    polling = false;
    setButtonsDisabled(false);
    if (payload.status === "completed") {
      setState("success", "同步完成", "知识库索引已更新。");
      window.setTimeout(() => window.location.reload(), 350);
      return;
    }
    setState(
      "failed",
      "同步失败",
      payload.error_summary || "知识库同步没有完成，可重新发起同步。",
      true,
    );
  };

  const poll = async (pollUrl) => {
    if (!pollUrl || polling) return;
    polling = true;
    setButtonsDisabled(true);
    try {
      const response = await fetch(pollUrl);
      const payload = await response.json().catch(() => null);
      if (!response.ok || !payload) throw new Error("无法读取知识库同步状态。");
      if (["queued", "running"].includes(payload.status)) {
        setState(
          payload.status,
          payload.status === "queued" ? "同步任务已排队" : "正在同步",
          payload.progress_log?.at(-1) || "正在读取知识库页面元数据。",
        );
        polling = false;
        window.setTimeout(() => poll(pollUrl), 650);
        return;
      }
      completePolling(payload);
    } catch (error) {
      polling = false;
      setButtonsDisabled(false);
      setState("failed", "同步状态未知", error.message, true);
    }
  };

  const startSync = async (mode) => {
    setButtonsDisabled(true);
    setState("queued", "正在认证预检", "正在检查本机凭据与当前知识库账号。");
    try {
      const payload = await postForm("/knowledge-base/sync", token, { mode });
      poll(payload.poll_url);
    } catch (error) {
      setButtonsDisabled(false);
      setState("failed", "同步失败", error.message, true);
    }
  };

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.dataset.syncMode || "incremental";
      if (mode === "full" && fullSyncDialog) {
        fullSyncDialog.showModal();
        return;
      }
      startSync(mode);
    });
  });

  confirmFullSync?.addEventListener("click", () => {
    fullSyncDialog?.close();
    startSync("full");
  });

  if (center.dataset.jobId && center.dataset.pollUrl) {
    poll(center.dataset.pollUrl);
  }
};

const initializeReportSyncPicker = () => {
  const center = document.querySelector("[data-report-sync-center]");
  const openButton = document.querySelector("[data-report-sync-open-dialog]");
  const dialog = document.querySelector("[data-report-sync-dialog]");
  if (!center || !openButton || !dialog) return;

  const select = dialog.querySelector("[data-report-sync-select]");
  const confirm = dialog.querySelector("[data-report-sync-confirm]");
  const message = dialog.querySelector("[data-report-sync-message]");

  const setMessage = (text, isError = false) => {
    message.textContent = text;
    message.classList.toggle("is-error", isError);
  };

  const setOptions = (candidates) => {
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "请选择日报日期";
    select.append(placeholder);
    candidates.forEach((candidate) => {
      const option = document.createElement("option");
      option.value = candidate.report_date;
      option.textContent = `${candidate.report_date} · ${candidate.customer_name}`;
      select.append(option);
    });
    select.disabled = candidates.length === 0;
    confirm.disabled = true;
  };

  const loadCandidates = async () => {
    select.disabled = true;
    confirm.disabled = true;
    setMessage("正在扫描日报.md 中最近 7 天可补交的日报…");
    const response = await fetch("/report-sync/candidates");
    const payload = await response.json().catch(() => ({ error: "服务器返回了无法解析的结果。" }));
    if (!response.ok) throw new Error(payload.error || `扫描失败（${response.status}）`);
    const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
    setOptions(candidates);
    setMessage(
      candidates.length
        ? "请选择一个日期后继续；仅显示日报.md 中扫描到的最近 7 日记录。"
        : "日报.md 中没有可补交的最近 7 日记录。",
      candidates.length === 0
    );
  };

  openButton.addEventListener("click", async () => {
    openButton.disabled = true;
    if (!dialog.open) dialog.showModal();
    try {
      await loadCandidates();
    } catch (error) {
      select.replaceChildren();
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "无法读取补交日期";
      select.append(placeholder);
      select.disabled = true;
      confirm.disabled = true;
      setMessage(error.message, true);
    } finally {
      openButton.disabled = false;
    }
  });

  select.addEventListener("change", () => {
    confirm.disabled = !select.value;
    if (select.value) setMessage("将打开该日报的源文件预览，您可继续选择需要补交的项目。");
  });

  confirm.addEventListener("click", async () => {
    if (!select.value) return;
    confirm.disabled = true;
    setMessage("正在核对所选日期是否仍来自日报.md…");
    try {
      const response = await fetch(
        `/report-sync/open?report_date=${encodeURIComponent(select.value)}`
      );
      const payload = await response.json().catch(() => ({ error: "服务器返回了无法解析的结果。" }));
      if (!response.ok) throw new Error(payload.error || `校验失败（${response.status}）`);
      window.location.assign(payload.day_url);
    } catch (error) {
      setMessage(error.message, true);
      confirm.disabled = false;
    }
  });
};

const initializeServiceModeFilter = () => {
  const openButton = document.querySelector("[data-service-mode-filter-open]");
  const dialog = document.querySelector("[data-service-mode-filter-dialog]");
  if (!openButton || !dialog) return;

  const period = dialog.querySelector("[data-service-mode-period]");
  const anchorField = dialog.querySelector("[data-service-mode-anchor-field]");
  const customRange = dialog.querySelector("[data-service-mode-custom-range]");

  const updateRangeFields = () => {
    const isCustomRange = period.value === "custom";
    anchorField.hidden = isCustomRange;
    customRange.hidden = !isCustomRange;
  };

  openButton.addEventListener("click", () => {
    updateRangeFields();
    if (!dialog.open) dialog.showModal();
  });
  period.addEventListener("change", updateRangeFields);
};

const initializeDateAttributions = () => {
  const dialog = document.querySelector("[data-date-attribution-dialog]");
  const facts = dialog?.querySelector("[data-date-attribution-facts]");
  const message = dialog?.querySelector("[data-date-attribution-message]");
  const confirm = dialog?.querySelector("[data-confirm-date-attribution]");
  const sources = document.querySelectorAll("[data-date-attribution-source]");
  const targets = document.querySelectorAll("[data-date-attribution-target]");
  let sourceDate = "";
  let targetDate = "";

  const clearTargetHighlight = () => {
    targets.forEach((target) => target.classList.remove("is-date-attribution-target"));
  };

  const canUseTarget = (target) => {
    const candidateDate = target.dataset.dateAttributionTarget || "";
    return Boolean(sourceDate && candidateDate && candidateDate < sourceDate);
  };

  sources.forEach((source) => {
    source.addEventListener("dragstart", (event) => {
      sourceDate = source.dataset.dateAttributionSource || "";
      event.dataTransfer?.setData("text/plain", sourceDate);
      if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
      source.classList.add("is-date-attribution-source");
    });
    source.addEventListener("dragend", () => {
      source.classList.remove("is-date-attribution-source");
      clearTargetHighlight();
    });
  });

  targets.forEach((target) => {
    target.addEventListener("dragover", (event) => {
      if (!canUseTarget(target)) return;
      event.preventDefault();
      target.classList.add("is-date-attribution-target");
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    });
    target.addEventListener("dragleave", () => {
      target.classList.remove("is-date-attribution-target");
    });
    target.addEventListener("drop", (event) => {
      if (!canUseTarget(target)) return;
      event.preventDefault();
      targetDate = target.dataset.dateAttributionTarget || "";
      sourceDate = event.dataTransfer?.getData("text/plain") || sourceDate;
      clearTargetHighlight();
      if (!dialog || !facts || !message || !confirm || !sourceDate || !targetDate || targetDate >= sourceDate) return;
      facts.replaceChildren();
      appendFact(facts, "实际补交日", sourceDate);
      appendFact(facts, "归属工作日", targetDate);
      message.textContent = "";
      message.classList.remove("is-error");
      confirm.disabled = false;
      dialog.showModal();
    });
  });

  confirm?.addEventListener("click", async () => {
    if (!sourceDate || !targetDate || !dialog || !message) return;
    const token = document.querySelector("[data-date-attribution-target]")
      ?.closest(".calendar-panel")
      ?.dataset.localToken;
    if (!token) {
      message.textContent = "本机请求令牌不可用，请刷新页面后重试。";
      message.classList.add("is-error");
      return;
    }
    confirm.disabled = true;
    try {
      await postForm("/date-attributions", token, {
        source_date: sourceDate,
        target_date: targetDate,
      });
      window.location.reload();
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("is-error");
      confirm.disabled = false;
    }
  });

  const revokePanel = document.querySelector("[data-date-attribution-revoke]");
  const revokeButton = revokePanel?.querySelector("[data-revoke-date-attribution]");
  const revokeMessage = revokePanel?.querySelector("[data-date-attribution-revoke-message]");
  revokeButton?.addEventListener("click", async () => {
    const target = revokePanel.dataset.targetDate || "";
    const token = revokePanel.dataset.localToken || "";
    if (!target || !token || !revokeMessage) return;
    revokeButton.disabled = true;
    try {
      await postForm(`/date-attributions/${encodeURIComponent(target)}/revoke`, token, {});
      window.location.assign("/");
    } catch (error) {
      revokeMessage.textContent = error.message;
      revokeMessage.classList.add("is-error");
      revokeButton.disabled = false;
    }
  });
};

const initializeCalendarDayStatuses = () => {
  const dialog = document.querySelector("[data-calendar-day-status-dialog]");
  const panel = document.querySelector(".calendar-panel");
  const facts = dialog?.querySelector("[data-calendar-day-status-facts]");
  const select = dialog?.querySelector("[data-calendar-day-status-select]");
  const message = dialog?.querySelector("[data-calendar-day-status-message]");
  const saveButton = dialog?.querySelector("[data-save-calendar-day-status]");
  const clearButton = dialog?.querySelector("[data-clear-calendar-day-status]");
  const controls = document.querySelectorAll("[data-calendar-day-status-open]");
  let reportDate = "";

  const resetMessage = () => {
    if (!message) return;
    message.textContent = "";
    message.classList.remove("is-error");
  };

  controls.forEach((control) => {
    control.addEventListener("click", () => {
      reportDate = control.dataset.calendarDayStatusDate || "";
      if (!dialog || !facts || !select || !reportDate) return;
      const existingStatus = control.dataset.calendarDayStatus || "";
      const isAutomaticStatus = [
        "adjusted_rest",
        "adjusted_workday",
        "statutory_holiday",
        "weekend_rest",
      ].includes(existingStatus);
      select.value = isAutomaticStatus ? "leave" : existingStatus || "leave";
      clearButton.hidden = !existingStatus || isAutomaticStatus;
      saveButton.hidden = false;
      saveButton.disabled = false;
      clearButton.disabled = false;
      facts.replaceChildren();
      appendFact(facts, "日期", reportDate);
      resetMessage();
      dialog.showModal();
    });
  });

  saveButton?.addEventListener("click", async () => {
    const token = panel?.dataset.localToken || "";
    if (!reportDate || !select?.value || !token || !message) return;
    saveButton.disabled = true;
    try {
      await postForm("/calendar-day-statuses", token, {
        report_date: reportDate,
        status: select.value,
      });
      window.location.reload();
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("is-error");
      saveButton.disabled = false;
    }
  });

  clearButton?.addEventListener("click", async () => {
    const token = panel?.dataset.localToken || "";
    if (!reportDate || !token || !message) return;
    clearButton.disabled = true;
    try {
      await postForm(`/calendar-day-statuses/${encodeURIComponent(reportDate)}/clear`, token, {});
      window.location.reload();
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("is-error");
      clearButton.disabled = false;
    }
  });
};

document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog")?.close());
});
initializeDayActions();
initializeJobCenter();
initializeArtifactCleanup();
initializeArtifactFilter();
initializeKnowledgeBaseSync();
initializeReportSyncPicker();
initializeServiceModeFilter();
initializeDateAttributions();
initializeCalendarDayStatuses();
