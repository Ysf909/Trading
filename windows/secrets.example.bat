@echo off
rem Your private keys. install.bat copies this file to windows\secrets.bat - edit THAT file.
rem Leave a value empty if you don't use it. Never share secrets.bat.

rem Telegram notifications (see docs\INSTALL_MT5.md, step 10)
set TELEGRAM_BOT_TOKEN=
set TELEGRAM_CHAT_ID=

rem Only if config.yaml sets broker.mt5_login (the bot logs in by itself)
set MT5_PASSWORD=

rem Only if config.yaml sets ai.enabled: true (Claude reviews every trade)
set ANTHROPIC_API_KEY=

rem Only for start_webhook.bat (same value as the TradingView indicator input)
set WEBHOOK_PASSPHRASE=
