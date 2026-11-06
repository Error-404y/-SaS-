from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


PALETTE = {
    'Alliance': ((249, 197, 74), (202, 143, 34)),
    'Events': ((77, 157, 225), (37, 102, 174)),
    'Deals': ((254, 107, 112), (204, 68, 72)),
    'Port Inspection': ((247, 107, 112), (203, 71, 76)),
    'Other': ((92, 194, 127), (40, 146, 84))
}
CATEGORY_ORDER = ('Port Inspection', 'Events', 'Alliance', 'Deals', 'Other')


def typeface(size, bold=False):
    paths = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation2/LiberationSans.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def fit_text(draw, value, width, size, bold=False, minimum=14):
    while size >= minimum:
        font = typeface(size, bold)
        if draw.textbbox((0, 0), str(value), font=font)[2] <= width:
            return font
        size -= 1
    return typeface(minimum, bold)


def ellipsis(draw, text, width, font):
    text = str(text)
    if draw.textbbox((0, 0), text, font=font)[2] <= width:
        return text
    while text and draw.textbbox((0, 0), text + '...', font=font)[2] > width:
        text = text[:-1]
    return text + '...'


def png(image):
    output = BytesIO()
    image.save(output, format='PNG', optimize=True)
    output.seek(0)
    return output


def event_banner(title, category, stage, starts_at=None, ends_at=None, server_now=None):
    server_now = datetime.now(timezone.utc) if server_now is None else server_now
    width, height = 1000, 300
    image = Image.new('RGB', (width, height), (62, 49, 41))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=18, fill=(76, 62, 52), outline=(151, 121, 84), width=3)
    draw.rounded_rectangle((13, 13, width - 13, 68), radius=12, fill=(95, 78, 62))
    draw.text((34, 24), 'EVENTS', font=typeface(30, True), fill=(255, 251, 238))
    draw.text((800, 30), 'SaS  EVENT CENTER', font=typeface(16, True), fill=(251, 218, 154))
    draw.rounded_rectangle((13, 75, width - 13, 139), radius=12, fill=(94, 173, 231), outline=(55, 128, 192), width=3)
    label = 'Server Time ' + server_now.strftime('%Y-%m-%d %H:%M:%S') + ' UTC'
    draw.text((48, 89), label, font=fit_text(draw, label, 910, 31, True), fill=(255, 255, 255), stroke_width=1, stroke_fill=(65, 115, 154))
    draw.rounded_rectangle((15, 148, width - 15, height - 18), radius=13, fill=(178, 207, 230))
    light, dark = PALETTE.get(category, PALETTE['Other'])
    bx0, bx1, by0, by1 = 33, width - 31, 165, 230
    draw.polygon(((bx0, by0), (bx1 - 29, by0), (bx1, (by0 + by1)//2), (bx1 - 29, by1), (bx0, by1)), fill=light, outline=dark, width=4)
    draw.line((bx0 + 10, by0 + 4, bx1 - 38, by0 + 4), fill=(255, 246, 222), width=2)
    event_name = ellipsis(draw, title, 820, typeface(30, True))
    draw.text((54, 177), event_name, font=typeface(30, True), fill=(255, 255, 255), stroke_width=1, stroke_fill=(72, 64, 62))
    stage_label = {
        'upcoming': 'UPCOMING', '24h': '24 HOURS BEFORE', '1h': '1 HOUR BEFORE',
        '15m': '15 MINUTES BEFORE', 'start': 'STARTING NOW', 'cancelled': 'CANCELLED'
    }.get(stage, 'EVENT REMINDER')
    draw.text((43, 247), stage_label, font=typeface(18, True), fill=(60, 85, 117))
    if starts_at is not None:
        start = datetime.fromtimestamp(int(starts_at), timezone.utc)
        date_string = start.strftime('%m/%d %H:%M:%S')
        if ends_at is not None:
            finish = datetime.fromtimestamp(int(ends_at), timezone.utc)
            date_string += '  -  ' + finish.strftime('%m/%d %H:%M:%S')
        date_string += ' UTC'
        label_font = fit_text(draw, date_string, 620, 18, True)
        tw = draw.textbbox((0,0), date_string, font=label_font)[2]
        draw.text((width - 36 - tw, 247), date_string, font=label_font, fill=(53, 80, 116))
    return png(image)


def weekly_calendar(events, first_day, server_now=None, timezone_label='UTC', demo=False):
    if server_now is None:
        server_now = datetime.now(timezone.utc)
    if server_now.tzinfo is None:
        raise ValueError('Server time must include a timezone.')
    first_day = first_day.date() if isinstance(first_day, datetime) else first_day
    days = [first_day + timedelta(days=i) for i in range(7)]
    end_day = days[-1] + timedelta(days=1)
    visible = []
    for row in events:
        start = datetime.fromtimestamp(int(row['starts_at']), server_now.tzinfo)
        finish = datetime.fromtimestamp(int(row['ends_at']), server_now.tzinfo)
        if start.date() < end_day and finish > datetime.combine(first_day, datetime.min.time(), server_now.tzinfo):
            visible.append((row, start, finish))
    visible.sort(key=lambda x: (CATEGORY_ORDER.index(x[0]['category']) if x[0]['category'] in CATEGORY_ORDER else 99, x[1], x[0]['title']))
    max_events = 13
    shown = visible[:max_events]
    groups = []
    for category in CATEGORY_ORDER:
        matched = [item for item in shown if item[0]['category'] == category]
        if matched:
            groups.append((category, matched))
    if not groups:
        groups = [('EVENTS', [])]
    lane = 78
    section_header = 45
    top = 373
    grid_height = sum(section_header + max(1, len(items)) * lane for _, items in groups)
    height = top + grid_height + 80
    width = 1120
    image = Image.new('RGB', (width, height), (60, 49, 43))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width - 1, 120), radius=17, fill=(85, 70, 56))
    draw.text((35, 33), 'EVENTS', font=typeface(58, True), fill=(255, 255, 251), stroke_width=2, stroke_fill=(69, 49, 43))
    draw.text((822, 51), 'SaS EVENT CENTER', font=typeface(21, True), fill=(243, 217, 160))
    draw.rounded_rectangle((12, 132, 272, 204), radius=19, fill=(241, 238, 226))
    draw.text((42, 149), 'CALENDAR', font=typeface(30, True), fill=(87, 67, 50))
    for i, (label, x, end) in enumerate((('ALLIANCE', 283, 547), ('EVENTS', 555, 816), ('DEALS', 824, 1109))):
        draw.rounded_rectangle((x, 138, end, 204), radius=17, fill=(146, 113, 87), outline=(208, 172, 120), width=2)
        draw.text((x + 23, 156), label, font=typeface(24, True), fill=(253, 245, 229))
    draw.rounded_rectangle((0, 206, width - 1, 281), radius=14, fill=(103, 176, 225), outline=(56, 129, 185), width=4)
    server_clock = server_now.strftime('%Y-%m-%d %H:%M:%S')
    draw.text((46, 223), 'Server Time  ' + server_clock + '  ' + timezone_label, font=fit_text(draw, 'Server Time  ' + server_clock + '  ' + timezone_label, 1000, 33, True), fill=(255, 255, 255), stroke_width=1, stroke_fill=(59, 108, 152))
    x0 = 22
    x1 = width - 22
    column_width = (x1 - x0) / 7
    draw.rounded_rectangle((x0 - 2, 286, x1 + 2, height - 27), radius=13, fill=(182, 211, 235), outline=(230, 236, 241), width=4)
    for col, day in enumerate(days):
        x = round(x0 + col * column_width)
        xnext = round(x0 + (col + 1) * column_width)
        if day == server_now.date():
            draw.rectangle((x, 290, xnext, height - 34), fill=(222, 191, 143))
        draw.line((x, 291, x, height - 34), fill=(137, 175, 206), width=2)
        selected = day == server_now.date()
        draw.rounded_rectangle((x + 4, 297, xnext - 4, 365), radius=9, fill=(230, 166, 68) if selected else (109, 154, 190), outline=(254, 228, 145) if selected else (122, 170, 210), width=2)
        draw.text((x + 16, 303), day.strftime('%a').upper(), font=typeface(19, True), fill=(255, 251, 236) if selected else (56, 84, 125))
        draw.text((x + 16, 326), day.strftime('%m/%d'), font=typeface(26, True), fill=(255, 255, 255), stroke_width=1, stroke_fill=(81, 94, 108))
    cursor = 373
    for category, items in groups:
        draw.rectangle((x0 + 2, cursor, x1 - 2, cursor + section_header), fill=(97, 149, 190))
        draw.rectangle((x0 + 10, cursor + 11, x0 + 18, cursor + 32), fill=(238, 243, 246))
        draw.text((x0 + 28, cursor + 6), category.upper(), font=typeface(23, True), fill=(247, 250, 252))
        cursor += section_header
        for row, start, finish in items or [(None, None, None)]:
            y = cursor
            draw.line((x0, y + lane - 1, x1, y + lane - 1), fill=(138, 177, 206), width=2)
            if row is None:
                draw.text((x0 + 31, y + 22), 'No events scheduled for these dates.', font=typeface(24, True), fill=(70, 101, 139))
                cursor += lane
                continue
            start_index = max(0, min(7, (start.date() - first_day).days))
            end_index = max(0, min(7, (finish.date() - first_day).days + 1))
            if end_index <= start_index:
                end_index = start_index + 1
            bx0 = round(x0 + start_index * column_width) + 5
            bx1 = round(x0 + end_index * column_width) - 8
            bar_top = y + 8
            bar_bottom = y + lane - 10
            light, dark = PALETTE.get(category, PALETTE['Other'])
            draw.polygon(((bx0, bar_top), (bx1 - 20, bar_top), (bx1, (bar_top + bar_bottom)//2), (bx1 - 20, bar_bottom), (bx0, bar_bottom)), fill=light, outline=dark, width=3)
            draw.line((bx0 + 6, bar_top + 3, bx1 - 25, bar_top + 3), fill=(255, 255, 255), width=2)
            available_width = max(45, bx1 - bx0 - 39)
            title = ellipsis(draw, row['title'], available_width, typeface(22, True))
            draw.text((bx0 + 12, y + 11), title, font=typeface(22, True), fill=(255, 255, 255), stroke_width=1, stroke_fill=(91, 79, 69))
            time_string = start.strftime('%m/%d %H:%M:%S') + '  -  ' + finish.strftime('%m/%d %H:%M:%S') + '  ' + timezone_label
            time_font = typeface(15, True)
            draw.text((bx0 + 12, y + 41), ellipsis(draw, time_string, available_width, time_font), font=time_font, fill=(45, 56, 70))
            cursor += lane
    footer_y = height - 79
    draw.rectangle((x0 + 1, footer_y, x1 - 1, height - 32), fill=(144, 186, 214))
    label = 'PREVIEW DATA  |  TIMES ARE EXAMPLES' if demo else 'All events are subject to the in-game schedule'
    draw.text((x0 + 20, footer_y + 14), label, font=fit_text(draw, label, 1000, 22, True), fill=(57, 86, 125))
    if len(visible) > max_events:
        draw.text((x1 - 255, height - 101), f'+ {len(visible) - max_events} more events', font=typeface(17, True), fill=(52, 77, 100))
    return png(image)
