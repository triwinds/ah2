import requests
import app


# chat_id = app.get('notify/chat_id')
chat_id = '524692710'


def send_by_tg_bot(title, content):
    # @shadowfox_MsgCat_bot
    result = requests.post('https://msgcat.shadowfox.workers.dev/sendMsg',
                           json={'chatId': chat_id, 'title': title, 'content': content})
    return result
