FROM aiogram/telegram-bot-api:latest

RUN mkdir -p /var/lib/telegram-bot-api
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8080
CMD ["/start.sh"]
