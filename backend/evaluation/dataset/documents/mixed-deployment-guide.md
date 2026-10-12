# Deployment Guide デプロイ手順

## 概要

本番環境へのデプロイは、GitHub Actions の CI/CD パイプラインから実行する。main ブランチへのマージ後、コンテナイメージが自動的にビルドされ、staging 環境に反映される。

## Helm chart の更新

アプリケーションの設定は Helm chart で管理する。values.yaml を変更した場合は、レビューで二人の approve を得てからマージすること。

## Canary release

本番リリースは canary 方式で行い、最初の30分間はトラフィックの10%だけを新しいバージョンに流す。エラー率が1%を超えた場合は自動的に rollback される。

## Rollback 手順

手動で rollback する場合は、helm rollback コマンドで直前の revision に戻す。データベースの migration を含むリリースは、rollback の前に必ず DBA に相談すること。

## Feature flag

未完成の機能は feature flag で無効化した状態でデプロイし、準備ができてから有効化する。
