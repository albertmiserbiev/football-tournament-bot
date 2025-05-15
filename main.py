import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters
)
import asyncio
from types import SimpleNamespace
from dotenv import load_dotenv
import os

# Load environment variables
if os.environ.get("RENDER") is None: load_dotenv("tkn.env")

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# States
SELECT_TEAMS, SELECT_MATCH, RECORD_RESULT, FINISH = range(4)

# Teams options
COLOR_OPTIONS = [
    ('🟦 Голубые', 'light-blue'), ('🔵 Синие', 'blue'), ('🟪 Фиолетовые', 'purple'),
    ('🩷 Розовые', 'pink'), ('🟥 Красные', 'red'), ('🟧 Оранжевые', 'orange'),
    ('🟨 Жёлтые', 'yellow'), ('🟩 Зелёные', 'green'), ('⬜ Белые', 'white'),
    ('⬛ Чёрные', 'black'), ('🌈 Цветные', 'rainbow')
]
KEY_TO_LABEL = {key: label.split()[-1] for label, key in COLOR_OPTIONS}
KEY_TO_EMOJI = {key: label.split()[0] for label, key in COLOR_OPTIONS}

# Generate live scoreboard with played matches and correct games count
async def generate_scoreboard(context: ContextTypes.DEFAULT_TYPE) -> str:
    # Initialize table
    table = {t: {'points': 0, 'scored': 0, 'conceded': 0, 'games': 0} for t in context.user_data['teams']}
    # Fill stats from match_log
    for round_num, t1, t2, x, y in context.user_data.get('match_log', []):
        table[t1]['scored'] += x; table[t1]['conceded'] += y; table[t1]['games'] += 1
        table[t2]['scored'] += y; table[t2]['conceded'] += x; table[t2]['games'] += 1
        if x > y:
            table[t1]['points'] += 3
        elif y > x:
            table[t2]['points'] += 3
        else:
            table[t1]['points'] += 1; table[t2]['points'] += 1
    # Sort by points, goal diff, scored
    standings = sorted(
        table.items(),
        key=lambda it: (it[1]['points'], it[1]['scored'] - it[1]['conceded'], it[1]['scored']),
        reverse=True
    )
    # Build table lines
    lines = ['*Текущая таблица:*', '```']
    header = f"{'№':<2} {'Команда':<12} {'И':<2} {'+/-':<3} {'Очки':<4}"
    lines.append(header)
    for i, (team, s) in enumerate(standings, 1):
        gd = s['scored'] - s['conceded']
        lines.append(f"{i:<2} {KEY_TO_LABEL[team]:<12} {s['games']:<2} {gd:<3} {s['points']:<4}")
    lines.append('```')
    # Append list of played matches formatted like finish
    match_rows = ['\n*Результаты:*']
    current_round = None
    for round_num, t1, t2, x, y in sorted(
            context.user_data.get('match_log', []), key=lambda item: (item[0], context.user_data['match_log'].index(item))):
        if round_num != current_round:
            current_round = round_num
            match_rows.append('')
        match_rows.append(f"{KEY_TO_LABEL[t1]} — {KEY_TO_LABEL[t2]} {x}:{y}")
    # Combine and return
    return "\n".join(lines + match_rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data['teams'] = []
    context.user_data['messages_to_delete'] = []
    context.user_data['prompt_message'] = None
    context.user_data['start_message'] = None
    await send_team_selection(update, context)
    return SELECT_TEAMS

async def send_team_selection(update, context):
    sel = context.user_data['teams']
    kb, row = [], []
    for label, key in COLOR_OPTIONS:
        text = ('✔️ ' if key in sel else '') + label
        row.append(InlineKeyboardButton(text, callback_data=key))
        if len(row) == 2:
            kb.append(row); row = []
    if row: kb.append(row)
    actions = [InlineKeyboardButton('❌ Отменить', callback_data='cancel')]
    if len(sel) >= 2:
        actions.insert(0, InlineKeyboardButton('✅ Создать', callback_data='done'))
    kb.append(actions)
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await update.callback_query.edit_message_text('Выберите команды, чтобы начать турнир:', reply_markup=markup)
    else:
        await update.message.reply_text('Выберите команды, чтобы начать турнир:', reply_markup=markup)

async def select_teams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer()
    choice = q.data
    if choice == 'cancel':
        await q.message.delete()
        await q.message.reply_text('Турнир отменён. Для создания турнира нажмите /start')
        return ConversationHandler.END
    sel = context.user_data['teams']
    if choice == 'done':
        teams = sel.copy()
        schedule = [(teams[i], teams[j]) for i in range(len(teams)) for j in range(i+1, len(teams))]
        context.user_data.update({
            'schedule': [], 'queue': schedule.copy(), 'all_matches': schedule.copy(),
            'round': 1, 'results': {}, 'match_log': [], 'timer_task': None,
            'scoreboard_message_id': None
        })
        buttons = [[InlineKeyboardButton(f"{KEY_TO_EMOJI[t1]} {KEY_TO_LABEL[t1]} vs {KEY_TO_EMOJI[t2]} {KEY_TO_LABEL[t2]}", callback_data=str(i))]
                   for i, (t1, t2) in enumerate(context.user_data['queue'])]
        buttons.append([InlineKeyboardButton('❌ Отменить', callback_data='cancel')])
        await q.edit_message_text(f'Круг 1: выберите, кто играет первым', reply_markup=InlineKeyboardMarkup(buttons))
        return SELECT_MATCH
    if choice in sel: sel.remove(choice)
    else: sel.append(choice)
    return await send_team_selection(update, context)

async def select_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer()
    if q.data == 'cancel':
        await q.message.delete()
        await q.message.reply_text('Турнир отменён. Для создания нового турнира нажмите /start')
        return ConversationHandler.END
    if q.data == 'finish':
        return await finish(update, context)
    idx = int(q.data)
    match = context.user_data['queue'].pop(idx)
    context.user_data['current'] = match
    context.user_data['schedule'].append(match)
    t1, t2 = match
    text = f"Введите счёт матча {KEY_TO_EMOJI[t1]} {KEY_TO_LABEL[t1]} vs {KEY_TO_EMOJI[t2]} {KEY_TO_LABEL[t2]} (например 2:1):"
    kb = [[InlineKeyboardButton('🏆 Завершить', callback_data='finish')]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['prompt_message'] = {'chat_id': q.message.chat.id, 'message_id': q.message.message_id}
    return RECORD_RESULT

async def handle_result_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer()
    if q.data == 'finish': return await finish(update, context)
    if q.data == 'cancel': return await cancel(update, context)
    return RECORD_RESULT

async def record_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    context.user_data['messages_to_delete'].append({'chat_id': chat_id, 'message_id': update.message.message_id})
    txt = update.message.text.strip()
    if txt == '/finish': return await finish(update, context)
    try:
        x, y = map(int, txt.split(':'))
        if x < 0 or y < 0: raise ValueError
    except:
        err_msg = await update.message.reply_text('Формат X:Y')
        context.user_data['messages_to_delete'].append({'chat_id': chat_id, 'message_id': err_msg.message_id})
        return RECORD_RESULT
    # delete old messages
    to_del = context.user_data.get('messages_to_delete', [])
    if context.user_data.get('prompt_message'): to_del.append(context.user_data['prompt_message'])
    if context.user_data.get('start_message'): to_del.append(context.user_data['start_message'])
    for msg in to_del:
        try: await context.bot.delete_message(chat_id=msg['chat_id'], message_id=msg['message_id'])
        except: pass
    context.user_data['messages_to_delete'] = []
    context.user_data['prompt_message'] = None
    context.user_data['start_message'] = None
    # record and show scoreboard
    m = context.user_data['current']; r = context.user_data['round']
    context.user_data['results'].setdefault(m, []).append((r, x, y))
    context.user_data['match_log'].append((r, m[0], m[1], x, y))
    if not context.user_data.get('timer_task'):
        context.user_data['timer_task'] = asyncio.create_task(schedule_auto_finish(context, chat_id))
    board_text = await generate_scoreboard(context)
    sb_id = context.user_data.get('scoreboard_message_id')
    if sb_id:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sb_id, text=board_text, parse_mode='Markdown')
    else:
        msg = await context.bot.send_message(chat_id=chat_id, text=board_text, parse_mode='Markdown')
        context.user_data['scoreboard_message_id'] = msg.message_id
    # next flow same as before
    if context.user_data['round'] == 1 and context.user_data['queue']:
        buttons = [[InlineKeyboardButton(f"{KEY_TO_EMOJI[a]} {KEY_TO_LABEL[a]} vs {KEY_TO_EMOJI[b]} {KEY_TO_LABEL[b]}", callback_data=str(i))]
                   for i, (a, b) in enumerate(context.user_data['queue'])]
        buttons.append([InlineKeyboardButton('🏆 Завершить', callback_data='finish')])
        await update.message.reply_text(f'Круг 1: выберите следующий матч:', reply_markup=InlineKeyboardMarkup(buttons))
        return SELECT_MATCH
    if context.user_data['round'] == 1:
        context.user_data['queue'] = context.user_data['schedule'].copy()
        context.user_data['round'] += 1
        start_msg = await update.message.reply_text(f'Начинается Круг 2!')
        context.user_data['start_message'] = {'chat_id': chat_id, 'message_id': start_msg.message_id}
    return await prompt_next(update, context)

async def prompt_next(update_obj, context):
    chat_id = update_obj.effective_chat.id
    if not context.user_data['queue']:
        context.user_data['queue'] = context.user_data['schedule'].copy()
        context.user_data['round'] += 1
        start_msg = await context.bot.send_message(chat_id=chat_id, text=f'Начинается Круг {context.user_data["round"]}!')
        context.user_data['start_message'] = {'chat_id': chat_id, 'message_id': start_msg.message_id}
    match = context.user_data['queue'].pop(0)
    context.user_data['current'] = match
    t1, t2 = match
    text = f"Матч {KEY_TO_EMOJI[t1]} {KEY_TO_LABEL[t1]} vs {KEY_TO_EMOJI[t2]} {KEY_TO_LABEL[t2]}:"
    kb = [[InlineKeyboardButton('🏆 Завершить', callback_data='finish')]]
    if hasattr(update_obj, 'message') and update_obj.message:
        msg = await update_obj.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        msg = await update_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['prompt_message'] = {'chat_id': chat_id, 'message_id': msg.message_id}
    return RECORD_RESULT

async def schedule_auto_finish(context, chat_id):
    await asyncio.sleep(2 * 60 * 60)
    if context.user_data.get('results'):
        fake_update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))
        await finish(fake_update, context)

async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    sb_id = context.user_data.get('scoreboard_message_id')
    if sb_id:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=sb_id)
        except: pass
    to_del = context.user_data.get('messages_to_delete', [])
    if context.user_data.get('prompt_message'): to_del.append(context.user_data['prompt_message'])
    if context.user_data.get('start_message'): to_del.append(context.user_data['start_message'])
    for msg in to_del:
        try: await context.bot.delete_message(chat_id=msg['chat_id'], message_id=msg['message_id'])
        except: pass
    # final standings same as before...
    table = {t: {'scored': 0, 'conceded': 0, 'points': 0, 'wins': 0, 'draws': 0, 'losses': 0, 'games': 0}
             for t in context.user_data['teams']}
    for (t1, t2), results in context.user_data['results'].items():
        for _, x, y in results:
            table[t1]['scored'] += x; table[t1]['conceded'] += y
            table[t2]['scored'] += y; table[t2]['conceded'] += x
            table[t1]['games'] += 1; table[t2]['games'] += 1
            if x > y:
                table[t1]['points'] += 3; table[t1]['wins'] += 1; table[t2]['losses'] += 1
            elif y > x:
                table[t2]['points'] += 3; table[t2]['wins'] += 1; table[t1]['losses'] += 1
            else:
                table[t1]['points'] += 1; table[t2]['points'] += 1
                table[t1]['draws'] += 1; table[t2]['draws'] += 1
    standings = sorted(table.items(), key=lambda it: (it[1]['points'], it[1]['scored']-it[1]['conceded'], it[1]['scored']), reverse=True)
    lines = [f"*🏆 Победили {KEY_TO_LABEL[standings[0][0]]}! 🏆*", '*\nИтоговая таблица:*', '```']
    lines.append(f"{'№':<2} {'Команда':<12} {'И':<2} {'+/-':<3} {'Очки':<4}")
    for i, (team, s) in enumerate(standings, 1):
        gd = s['scored'] - s['conceded']
        lines.append(f"{i:<2} {KEY_TO_LABEL[team]:<12} {s['games']:<2} {gd:<3} {s['points']:<4}")
    lines.append('```')
    match_rows = ['\n*Результаты:*']
    current_round = None
    for round_num, t1, t2, x, y in sorted(context.user_data['match_log'], key=lambda x: (x[0], context.user_data['match_log'].index(x))):
        if round_num != current_round:
            current_round = round_num; match_rows.append('')
        match_rows.append(f"{KEY_TO_LABEL[t1]} — {KEY_TO_LABEL[t2]} {x}:{y}")
    text = "\n".join(lines + match_rows + ["", "Турнир завершен! Для создания нового турнира нажмите /start"])
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query: await update.callback_query.message.delete()
    await context.bot.send_message(chat_id=update.effective_chat.id, text='Отменено. Для создания нового турнира нажмите /start')
    return ConversationHandler.END

# Entrypoint unchanged
if __name__ == '__main__':
    token = os.getenv("BOT_TOKEN")
    app = ApplicationBuilder().token(token).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECT_TEAMS: [CallbackQueryHandler(select_teams)],
            SELECT_MATCH: [CallbackQueryHandler(select_match)],
            RECORD_RESULT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, record_result),
                CallbackQueryHandler(handle_result_buttons)
            ],
            FINISH: []
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start)]
    )
    app.add_handler(conv)
    app.run_polling()
