# 完整部署指南

## 1. 安装 flyctl 并注册

```bash
brew install flyctl
flyctl auth signup    # 免费账号即可
```

## 2. 创建 GitHub App

这是最关键的步骤。Agent 需要一个 GitHub App 才能贴 review comment。

1. 打开 https://github.com/settings/apps/new
2. 填写：
   - **Name**: `open-reviewer-bot`
   - **Homepage URL**: `https://github.com/Oxygen56/open-reviewer`
   - **Webhook URL**: `https://open-reviewer.fly.dev/webhook`
   - **Webhook Secret**: 随便生成一个字符串（记下来，后面要设置到 `WEBHOOK_SECRET`）

3. **Permissions（仓库权限）**:
   - `Pull requests` → Read & Write
   - `Contents` → Read
   - `Issues` → Read & Write

4. **Subscribe to events**:
   - ✅ Pull request
   - ✅ Pull request review

5. 创建后，生成一个 **Private key** (.pem 文件)，下载保存为 `open-reviewer.private-key.pem`

6. 记下 **App ID**（位于页面顶部）

## 3. 安装到你的仓库

1. GitHub App 设置页面 → **Install App**
2. 选择你的账户或组织
3. 选择仓库：至少选 `Oxygen56/open-reviewer` 用于测试

## 4. 设置环境变量并部署

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GITHUB_APP_ID=123456           # 你的 App ID
export GITHUB_PRIVATE_KEY="$(cat open-reviewer.private-key.pem)"
export WEBHOOK_SECRET=你刚才生成的那个字符串

cd ~/projects/open-reviewer  # or wherever you cloned
./deploy.sh
```

## 5. 测试

1. 在安装了 App 的仓库里创建一个 PR
2. Agent 会在 30 秒内自动贴 review comment
3. 或者手动触发：`curl -X POST https://open-reviewer.fly.dev/review/Oxygen56/open-reviewer/1`

## 6. 查看日志

```bash
flyctl logs
```
