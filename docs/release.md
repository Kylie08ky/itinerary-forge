# 分支与发布

| 分支 | 用途 | 在哪里 |
|---|---|---|
| `develop` | 日常开发和迭代，完整历史 | 只在本机，不上传（历史里有真实邮箱） |
| `main` | 展示给别人看的，只在发布时更新 | GitHub 公开仓库 `origin/main` |

## 日常

- 在 `develop` 上工作，你说「提交」才提交；每轮在 `data/changelog.json` 最前面加一条，这就是「每轮做了什么」的记录。
- 不碰 `main`，不 push，除非你明确说「发布」。

## 什么时候发布到 main

五条都满足：

1. `specs/05-progress-checklist.md` 里这个版本的项你都确认了。
2. 整套单元测试通过；浏览器验收通过（偶发失败单独重跑过）。
3. 公开前检查没有命中（见下）。
4. `README.md`、`README.zh-CN.md`、`VALIDATION.md` 和现状一致。
5. 文档体检通过：`python scripts/check_docs.py`；评测报告没过期：`python scripts/build_eval_report.py --check`（过期就重新生成）。

## 怎么发布

```bash
git checkout main
git read-tree --reset -u develop      # main 的内容 = develop 当前的内容
git add -A
git -c user.name=Kylie08ky -c user.email=156586258+Kylie08ky@users.noreply.github.com commit -m "Release v0.5.0"
git tag v0.5.0
git push origin main --tags           # 你确认后才推
git checkout develop
```

GitHub Release 的说明 = 自上个 tag 以来 `data/changelog.json` 里的条目。版本号：清单里完成一个版本或一大块 +0.1，只修问题 +0.0.1。

## 公开前检查

- 密钥：`git grep -n -I -E "AMAP_(WEB|JS)_(KEY|SECRET)=[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{20,}"` 不应有命中（测试里的假值和 `.env.example` 的注释除外）。
- 不该有的文件：`git ls-files | grep -E "(^|/)\.env($|\.)|plans\.db|\.claude/"` 应为空。
- 靠 `.gitignore` 排除：`.env*`、`data/plans/`、`data/generated/`、`data/tile-cache/`、`.claude/`。

## 参考

TREK（`main` + `dev`，手写 Release，`v` + 语义化版本）、TRIP（语义化版本，手写 Release，打 tag 触发镜像发布）。两者都不另写 CHANGELOG 文件，发布说明放在 GitHub Releases。
