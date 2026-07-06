# GitHub Pages 免费托管

## 第一次设置

1. 在 GitHub 新建一个空仓库，例如 `jrtt`。
2. 不要勾选初始化 README、`.gitignore` 或 license。
3. 在当前目录运行：

```bash
chmod +x scripts/deploy_github_pages.sh
scripts/deploy_github_pages.sh 你的GitHub用户名 jrtt
```

脚本会：

- 用 GitHub Pages 地址重新生成 `public/feed.xml`
- 初始化本地 git 仓库
- 添加 GitHub remote
- 把需要提交的文件加入暂存区

然后按脚本输出执行：

```bash
git commit -m "Deploy JR Toutiao content site"
git push -u origin main
```

## 启用 Pages

推送后，打开 GitHub 仓库：

```text
Settings -> Pages -> Build and deployment -> Source -> GitHub Actions
```

等待 Actions 跑完，内容源地址就是：

```text
https://你的GitHub用户名.github.io/jrtt/feed.xml
```

## 后续更新

生成新文章并更新内容源：

```bash
python3 src/jrtt/cli.py auto --count 1 --deploy
```

只同步已有最新文章：

```bash
python3 src/jrtt/cli.py deploy
```

## 注意

- `.env` 不会提交，API key 留在本机。
- `data/jrtt.db` 不会提交。
- 发布到头条前仍然建议人工检查事实、数字、标题和敏感表达。
