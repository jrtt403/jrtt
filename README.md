# 今日头条 AI 辅助创作 MVP

这个目录用于维护工作 `019f356c-28cb-7281-aa26-15990fa94671` 的方案与代码。

目标不是全自动搬运新闻，而是做一套稳妥的半自动创作流程：

```text
抓取热点 -> 去重入库 -> 选题评分 -> 生成事实卡片/写作角度 -> 人工审核 -> 手动发布
```

## 当前能力

- 从 `config/sources.json` 配置的 RSS 源抓取热点
- 使用 SQLite 保存热点数据
- 按热度、时效、普通人相关性、可解释空间、定位匹配、合规风险做基础评分
- 生成今日头条图文草稿 Markdown 骨架
- 调用 API 生成事实卡片、大纲和可编辑初稿
- `draft --ai` 会尽量抓取原文页面，给模型补充可核验文本

## 快速开始

```bash
python3 src/jrtt/cli.py fetch
python3 src/jrtt/cli.py list --limit 10
python3 src/jrtt/cli.py draft --item-id 1
```

接入 API 后，可以生成 AI 辅助材料：

```bash
export OPENAI_API_KEY="你的 OpenAI API Key"
python3 src/jrtt/cli.py draft --item-id 1 --ai
```

如果某些来源页面抓取困难，可以只用 RSS 标题和摘要：

```bash
python3 src/jrtt/cli.py draft --item-id 1 --ai --no-enrich
```

自动抓取、选题并直接生成完整文章：

```bash
python3 src/jrtt/cli.py auto --count 1
```

默认要求文章不低于 1000 个字符；如果生成偏短，程序会自动扩写。也可以手动调整：

```bash
python3 src/jrtt/cli.py auto --count 1 --min-article-chars 1500
```

自动生成的完整文章会输出到：

```text
articles/
```

## 自动化发布

文章发布不建议使用模拟登录脚本。当前更稳的路线是官方“网站内容源/RSS 同步发文”：

```bash
python3 src/jrtt/cli.py publish --article latest --base-url https://你的公网域名
```

这会生成：

```text
public/feed.xml
public/articles/*.html
```

然后把 `public/` 部署到公网静态网站，在头条号后台添加内容源地址：

```text
https://你的公网域名/feed.xml
```

也可以生成文章后自动更新内容源文件：

```bash
python3 src/jrtt/cli.py auto --count 1 --publish-feed --base-url https://你的公网域名
```

当前 GitHub Pages 已配置好后，可以一条命令完成生成、更新内容源、提交并推送：

```bash
python3 src/jrtt/cli.py auto --count 1 --deploy
```

也可以用脚本：

```bash
scripts/auto_publish.sh
```

只把已有最新文章重新同步到 GitHub Pages：

```bash
python3 src/jrtt/cli.py deploy
```

当前默认内容源地址：

```text
https://jrtt403.github.io/jrtt/feed.xml
```

注意：抖音开放平台的头条 OpenAPI 当前面向视频发布，暂不支持头条文章和微头条。图文文章优先使用头条号后台手动发布或网站内容源同步。

### Playwright 自动发布到头条号

如果内容源同步不可用，也可以用浏览器自动化填充头条号后台。首次运行建议使用有头模式，手动完成登录：

```bash
python3 scripts/toutiao_publish_playwright.py
```

登录态会保存在本地：

```text
data/toutiao_profile/
```

确认能打开预览后，再执行真实提交：

```bash
python3 scripts/toutiao_publish_playwright.py --confirm-publish
```

锁屏或定时任务场景可以尝试无头模式：

```bash
python3 scripts/toutiao_publish_playwright.py --headless --confirm-publish
```

也可以一条命令完成“生成文章、部署 GitHub Pages、提交头条后台”：

```bash
scripts/toutiao_auto_publish.sh
```

默认每天批量生成并发布 10 篇文章，每篇不少于 1000 字。可以用环境变量调整：

```bash
JRTT_AUTO_COUNT=10 \
JRTT_CANDIDATE_LIMIT=50 \
JRTT_TOUTIAO_INTERVAL_SECONDS=120 \
scripts/toutiao_auto_publish.sh
```

定时任务默认使用无头浏览器；首次登录或排查问题时可以强制打开可见浏览器：

```bash
JRTT_TOUTIAO_HEADLESS=0 scripts/toutiao_auto_publish.sh
```

注意：这是非官方浏览器自动化，不等于官方 API。登录过期、验证码、风控、后台改版、封面要求变化都可能导致失败；失败时会保存截图到 `data/toutiao_last.png`。

没有公网域名时，可以用 GitHub Pages 免费二级域名托管内容源：

```bash
chmod +x scripts/deploy_github_pages.sh
scripts/deploy_github_pages.sh 你的GitHub用户名 jrtt
```

详细步骤见 [docs/GITHUB_PAGES.md](docs/GITHUB_PAGES.md)。

默认会跳过抓不到原文的聚合页；如果你想强制只靠 RSS 标题和摘要生成，可以用：

```bash
python3 src/jrtt/cli.py auto --count 1 --allow-unenriched
```

默认也会避开战争、冲突、伤亡等高风险选题；如果确实要生成这类文章，可以显式加：

```bash
python3 src/jrtt/cli.py auto --count 1 --allow-risky
```

也可以在当前目录创建 `.env`：

```text
OPENAI_API_KEY=你的 OpenAI API Key
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
OPENAI_MODEL=glm-4.7-flash
```

如果切回 OpenAI 官方端点，可选指定 OpenAI 模型：

```bash
export OPENAI_MODEL="gpt-5.5"
```

默认数据库在：

```text
data/jrtt.db
```

草稿会输出到：

```text
drafts/
```

## 日常流程

```text
08:00 抓取热点
08:10 查看 top 10 选题
08:30 选择 1 个生成草稿
09:00 人工补观点、核事实、删风险表达
10:30 手动发布到今日头条
次日复盘展现、阅读、互动、收益
```

## 合规原则

- AI 只做资料员、编辑助理和初稿助手
- 不做未经核实爆料
- 不做标题党、煽动性表达、阴谋论
- 财经、医疗、法律、时政等高风险内容必须人工复核
- 文章要有自己的解释框架，避免洗稿和简单改写

## 后续维护方向

- 增加多来源交叉验证和热点聚类
- 增加今日头条、百度、微博、抖音热榜采集模块
- 增加文章复盘表：展现量、阅读量、点击率、评论、收藏、收益
- 增加 Web 控制台
- 研究官方内容源接入，不建议一开始使用非官方批量自动发布
