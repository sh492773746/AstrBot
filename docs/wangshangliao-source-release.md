# 旺商聊源码发布与完整性清单

更新：2026-10-23。本文定义源码入库和发布检查，不声明已经推送到远端或全部实群验收通过。
当前规则以[规则与AI分权](zh/platform/wangshangliao-rules-ai.md)和对应实现为准；
加载、配置读回、平台回执及手机观察分别记录于[维护说明](wangshangliao-maintenance.md)。

## 必须一并入库的实现

| 功能层 | 源码与必要配套 |
| --- | --- |
| 接入器与可信身份 | `astrbot/core/platform/sources/wangshangliao/`：登录、身份绑定、入站账本、收发、引用和结构化@、目录与权限 |
| 动作与持久化 | 同目录中的动作授权、自动审核、递进禁言、黑名单、异步数据库、调度和未知回执保护 |
| 插件与控制面 | `astrbot/builtin_stars/wangshangliao_moderation/`：固定命令、群管理、黑白名单、处罚查询／解禁、私信会话及控制面校验 |
| 图文与定时 | 插件中的关键词回复、私有素材、图片录入、去重与安全清理、先图后文、最多3图轮换、三条独立错峰计划 |
| 其他已实现业务 | 插件中的开通、只读管理员指引、按群AI与知识、额度、邀请归属、结账登记、活动及其权限和审计模块 |
| 管理页 | `dashboard/plugin-pages/wangshangliao/`及共用组件、引用表、页面入口、Vite和TypeScript配置 |
| 运行依赖 | `pyproject.toml`、依赖说明、`dashboard/package.json`、包管理锁文件和所有被源码引用的静态资源 |
| 构建与部署 | 插件页面构建配置、`dashboard/scripts/`、必要的`hatch_build.py`及部署模板；保留源码构建路径 |
| 核心接线 | `astrbot/core/`和`astrbot/dashboard/`中实际被上述功能使用的平台、组件、管线、后台API和生命周期修改 |
| 回归与使用帮助 | 对应Python及前端测试、原始中英文文档、教程生成器、文档目录与AI索引 |

不能只提交已跟踪的旧文件：新增模块、管理页、构建入口和测试同样必须核对。
不能以一份编译后的`index.html`代替管理页源码，也不能漏掉插件实际加载的自包含页面。
源码入库不等于本机业务设置或账号状态会在新部署中自动恢复。

## 不上传的内容

- 账号会话、令牌、密码、私钥、生产环境文件、真实联系人和聊天正文。
- 运行数据库、账本、素材存储、用户上传图片、测试群截图、日志及私有备份。
- `node_modules`、虚拟环境、缓存、开发工作树及可重建的整站`dist`目录。
- 真实群规则、名单、财务和活动数据导出；公开说明只保留必要的匿名化操作范围。

产品内置静态资源与离线UI夹具截图不是用户上传素材，应按明确路径白名单纳入；
不要为解决遗漏而取消所有图片或运行目录的忽略规则。
提交前扫描候选文件及Git索引，检查高风险文件名、凭据模式、超大文件和缺失的本地引用。
不要通过查看或复制真实业务数据库来生成文档、测试数据或样例配置。

## 构建与检查

在仓库根目录运行文档生成器，在`dashboard`中使用项目声明的Node及包管理环境：

```sh
node dashboard/scripts/build-wangshangliao-guide.mjs
.venv/bin/python scripts/build_wsl_guide_pdf.py
node --test dashboard/scripts/build-wangshangliao-guide.test.mjs
.venv/bin/pytest -q tests/test_wangshangliao_member_blacklist.py \
  tests/test_wangshangliao_member_list_removal.py \
  tests/test_wsl_member_list_storage_20261023.py
cd dashboard
npm run build:wangshangliao-page
npm run typecheck
```

PDF构建使用本机Playwright Chromium，必要时设置`WSL_GUIDE_CHROMIUM`。
教程源、HTML、索引和PDF须同批重建；文档生成不运行真实机器人动作。
仅更新文档时按维护说明发布静态文档，不重启消息服务。
接入器／核心代码的加载仍遵守完整重启要求，不能用Git提交或插件热重载替代。

## Git与验收边界

推送前核对目标仓库、可见性、分支、远端历史及提交作者。
本地独立基线不能强推覆盖已有远端主分支；使用独立分支并保留可审查的差异。
当远端另含本机没有的插件或新版本时，本机生产快照必须放入独立快照分支，
不能将整个生产树覆盖到远端主分支，也不能将“快照已上传”说成“主分支已合并”。
提交后检查Git索引无遗漏；推送后比较本地提交SHA与远端分支SHA，
并核对关键模块、管理页和文档能从远端提交读取。
没有远端地址、写权限或可靠回执时，明确报告未上传，不能写成“已提交GitHub”。

离线回归、构建、配置读回、真实收件、平台处罚和手机显示是不同证据层级。
2026-10-23 23:39的名单移除与三群7天配置对齐没有执行真实处罚；
产品默认仍为29天，名单加入仍不立即处罚，后续可信真实重入才禁言。
不要因文档更新或仓库上传而修改正式群规则、启用测试账号、开放窗口或重试未知动作。

## 本轮发布前检查（2026-10-23）

- 旺商聊Python专项：**8129通过、55条件跳过、2条现有弃用警告**，364.16秒。
  条件跳过不算通过，不替代真实平台或手机客户端验收。
- 旺商聊前端单元回归：**203通过**；独立文档生成器回归**5通过**。
- 插件及完整Dashboard的TypeScript类型检查和隔离输出目录构建通过；
  插件源码重建产物与当时正在提供的自包含管理页逐字节一致。
- 接入器和插件范围Ruff检查**0问题**，不据此宣称全仓全部模块都已验收。
- **49篇**本地教程、**6份**PDF重建；49篇教程在390px／1440px检查
  无脚本错误、缺图或横向溢出。
- 公开副本保留功能源码，仅清空一处旧验证码示例令牌；
  生产账号凭据、运行数据和上传素材未进入副本，生产源码未因脱敏被改写。
  已逐条审查扫描器误报，最终扫描另以原始结果核验。

上述检查不执行生产重启或群内处罚。最终远端分支、提交SHA及内容清单以实际上传回执为准，
本文不预写推送成功，也不宣称远端主分支已合并。

## English Release Boundary

Publish the complete adapter, plugin, dashboard sources, required static assets,
build inputs, shared-core wiring, tests and bilingual documentation together.
Keep credentials, sessions, production databases, customer content, uploads,
private backups and rebuildable caches out of Git. Regenerate guides and PDFs
from maintained sources; built pages do not replace their sources.
Use a reviewable branch when local and remote histories differ, never force-push
over a remote default branch. Verify the remote commit SHA and key paths after
upload. A Git commit or successful build is not live-sanction acceptance.
The latest seven-day setting is scoped configuration, not the29-day default.
