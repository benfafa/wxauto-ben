// 微信群聊 preset 的执行期安全边界：拒绝一切工具调用。
//
// 为什么不能只靠 preset 不挂载工具：DSH 的 profile/全局层会继承
// `read_mcp_resource`、`working_directory`、dev_* 等工具，MCP 服务器也可能由用户
// 全局挂载。wx-chat 是纯文本回复模式，不需要任何工具，所以这里直接在
// tools.guard 层拒绝所有执行；restrict 只负责尽量从 schema 里隐藏已知的全局工具。
//
// 安装脚本会把本文件复制到 DSH profile 目录，preset 行用 ./wx-bridge-tool-guard.mjs 引用。

export const name = 'wx-tool-guard'

export const inject = ['tools']

// 只用于减少 schema 噪声；真正的边界是下面的 guard（deny all）。
const KNOWN_GLOBAL_TOOLS = [
  // 全局继承
  'list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource',
  'working_directory',
  // shell / 文件 / 终端
  'pwsh', 'bash', 'terminal_open', 'terminal_send', 'terminal_read', 'terminal_signal', 'terminal_close', 'terminal_list',
  'read', 'read_image', 'write', 'edit', 'str_replace_editor', 'glob', 'grep',
  'load_workspace_dependencies', 'create_worktree',
  // 派生 agent / 编排
  'subagent', 'subagent_fork', 'list_subagent_models',
  'subagent_codex', 'subagent_claude_code',
  'send_message', 'interrupt_agent', 'list_agents', 'wait_agent',
  'spawn_teammate', 'team_task_create', 'team_task_get', 'team_task_list', 'team_task_update',
  'workflow', 'ralph',
  // 会话/宿主状态
  'cordis_inspect_list', 'cordis_inspect_query',
  'session_search', 'session_trace', 'session_event_read', 'session_event_search', 'session_event_trace',
  'dsh_snapshot', 'dsh_rollback', 'incident_resolved',
  // 插件/调度/任务
  'plugin_manager', 'plugin_market',
  'schedule_create', 'schedule_list', 'schedule_update', 'schedule_delete',
  'job_list', 'job_output', 'job_kill',
  'skill', 'present', 'todo_write', 'ask_user_question',
  'get_goal', 'create_goal', 'update_goal',
  // 开发注入器
  'dev_build_plugin', 'dev_clear_routes', 'dev_fix_patch', 'dev_heal_links',
  'dev_inject_plugin', 'dev_injected_list', 'dev_install_package', 'dev_plugin_status',
  'dev_release_plugin', 'dev_reload_package', 'dev_scaffold_plugin', 'dev_self_test',
  'dev_stage_add', 'dev_stage_call', 'dev_stage_demote', 'dev_stage_list',
  'dev_stage_promote', 'dev_uninject_plugin',
  'dev_mode_set', 'dev_mode_status', 'dev_mode_subagent'
]

const logged = new Set()

export function apply(ctx) {
  for (const toolName of KNOWN_GLOBAL_TOOLS) {
    try {
      ctx.tools.restrict({ deny: [toolName] })
    } catch {
      // 当前 DSH 版本未注册该名字时会抛错；逐个跳过，不影响 guard。
    }
  }

  ctx.tools.guard((exec) => {
    const toolName = typeof exec?.name === 'string' && exec.name ? exec.name : '(unknown)'
    if (!logged.has(toolName)) {
      logged.add(toolName)
      console.error(`[wx-tool-guard] 已拒绝工具调用：${toolName}（微信群聊机器人是纯文本模式）`)
    }
    return `微信群聊机器人是纯文本回复模式，禁止执行工具 "${toolName}"`
  })
}
