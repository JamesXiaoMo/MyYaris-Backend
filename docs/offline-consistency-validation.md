# 离线下载任务一致性修复：fake 验收记录

本记录对应对已拒绝基线 `5953fb1` 的后端修复。该基线及旧“247 项通过”不作为本次验收证据。最终提交的文件清单以 Git 为准。

## 本次验证

在 backend 仓库运行：

```sh
.venv/bin/python -m pytest tests/test_offline.py tests/test_offline_concurrency.py -q
.venv/bin/python -m pytest -q
```

- 离线专项：**54 passed**。
- 全量后端：**288 passed，26 warnings**。警告为现有 Starlette/FastAPI/httpx 接口弃用提示，无测试失败。
- `git diff --check`：通过。
- 工作区既有 `tools/check_public_privacy.py` 对 backend 跟踪文件检查：**0 finding(s)**。该扫描范围有限，不代替完整安全审计。
- 数据使用临时 SQLite 和 fake 歌单/回执；多 Session 测试使用独立真实 SQLite 连接及 barrier 同步，不依赖测试客户端共享连接来模拟竞争。

## 可审核证据

| 验收点 | 测试证据 |
| --- | --- |
| 显式不限量 WiFi 确认、完整固定 revision 歌单含缺元数据 ID、251 首跨批次覆盖 | `test_explicit_confirmation_and_full_favorites`、`test_complete_playlist_larger_than_one_work_batch` |
| 当前 Owner、账号、设备绑定、普通账号/phone/admin 不能写车机回执、cookie CSRF | `test_current_owner_required_for_all_reads_and_car_writes`、`test_dynamic_admin_account_isolation_at_api`、`test_device_spoof_rejected_and_alias_accepted`、`test_role_boundary`、`test_cookie_writes_require_existing_csrf_guard` |
| 暂停/取消使旧租约失效，取消不可恢复，网络过期/换网/授权轮换须显式恢复 | `test_pause_cancel_invalidate_inflight_and_block_execution`、`test_gate_denial_persists_pause_and_never_auto_resumes`、`test_network_lost_requires_explicit_resume_and_keeps_missing_work` |
| 持久跨任务库存与去重、过期只做本地核验、撤销后旧回执不复活 | `test_cross_job_dedup_durable_and_revoke_all_old_references`、`test_stale_inventory_requires_local_verify_not_download`、`test_verify_missing_file_revokes_instead_of_implicit_download`、`test_inventory_restart_and_key_isolation` |
| 拔卡/换卡覆盖同车所有账号/Owner/歌曲，另一车辆不受影响 | `test_card_removal_revokes_other_account_owner_history`、`test_card_switch_invalidates_old_work`、`test_card_removal_does_not_invalidate_other_vehicle` |
| 有界失败/超时重试、租约不超过授权到期、错误响应不回显私有输入 | `test_bounded_failures_and_no_raw_diagnostics`、`test_expired_lease_rejected_and_retries_bounded`、`test_claim_lease_cannot_outlive_network_confirmation`、`test_receipt_rejects_incomplete_or_private_fields` |
| 多 Session 同任务/跨任务认领去重、最后批次与幂等创建竞争、回执 CAS | `test_concurrent_claims_same_job`、`test_concurrent_claims_across_jobs`、`test_concurrent_create_last_batch`、`test_concurrent_idempotent_create`、`test_receipt_cas_with_preloaded_sessions` |
| 暂停与认领、心跳与超时重领、撤销与核验、授权轮换与认领的竞争 | `test_pause_vs_claim_is_serializable`、`test_heartbeat_vs_expired_reclaim`、`test_revoke_vs_reverification_cannot_resurrect`、`test_confirmation_rotation_vs_claim_cannot_inherit_new_grant` |
| 分页 revision 一致性、取消任务不被其他成功任务复活 | `test_pinned_page_rejects_stale_session_revision`、`test_cancelled_job_does_not_inherit_later_receipt` |
| 原子升级/迁移中断回滚/重复迁移、回执库存失败整组回滚 | `test_rejected_baseline_migration_is_repeatable`、`test_receipt_inventory_failure_rolls_back_entire_unit` |

接口字段、状态机与迁移行为详见 [API 参考](API参考.md#十二离线-sd-下载模块任务一致性修订契约)。

## 未执行与后续边界

本次仅 backend 代码、契约文档、fake 测试；没有独立部署构建或生产迁移，没有读取生产凭据/数据库，没有真实音源下载、音乐删除或车辆播放/导航/OBD 操作。没有修改车机或 iOS。

用户当前尚未确认不限量 WiFi。后端不能凭 epoch/SSID 证明不限量，也不能中断车机已在途字节；车机实际网络判断、SAF 原子落盘/seek/rename、本地优先与 iOS 状态展示尚需另行整合及真机验收。这些项目不计作本次已完成。
