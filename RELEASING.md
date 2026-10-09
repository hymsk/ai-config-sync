# 发布指南

## 状态

- 仓库当前为私有未发布基线：版本 `0.1.0`，无 tag，未发布到任何 package registry。
- 目标分发方式：源码仓库 + `python3 -m pip install .`；是否发布 PyPI 由用户另行明确授权。

## 本地门禁（每次改动后）

```bash
python3 -m unittest discover -s tests -t .
git diff --check
```

- 全部测试使用本地 bare 仓库与注入式 Layout，不访问真实远端、不碰生产目录。
- 涉及 SPEC 行为变更时，先更新 `SPEC.md` 再改实现，并同步双语 README 与 `CHANGELOG.md`。

## 发布前检查（须明确授权）

1. 复核完整 diff、版本号、`LICENSE`、SPEC/README/CHANGELOG 与实现一致。
2. 确认配置仓库示例与种子模板不含真实凭据、内部主机名或本机绝对路径。
3. `python3 -m build` 产出 sdist/wheel 并在干净虚拟环境安装验证入口命令可用。

## 授权边界

- Git 提交、推送、打 tag、创建 GitHub Release、发布 PyPI 是互相独立的授权范围；未获明确授权不执行其中任何一步。
- 不移动既有 tag，不改写已公开历史，不 force push。
- 发布失败时保留现场并报告准确状态，不盲目重试或覆盖版本。
- 版本策略：`0.x` 为早期版本，不承诺向后兼容；公开基线后的兼容性承诺在 `CHANGELOG.md` 中另行声明。
