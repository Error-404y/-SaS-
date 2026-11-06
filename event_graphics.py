from io import BytesIO
from PIL import Image, ImageDraw, ImageFont


def typeface(size, bold=False):
    paths = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def fit_text(draw, text, width, size, bold=False):
    while size > 18:
        chosen = typeface(size, bold)
        if draw.textbbox((0, 0), text, font=chosen)[2] <= width:
            return chosen
        size -= 2
    return typeface(size, bold)


def event_banner(title, category, stage):
    width, height = 1000, 300
    image = Image.new('RGB', (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            level = x / width
            pixels[x, y] = (11 + int(12 * level), 27 + int(28 * level), 53 + int(38 * level))
    draw = ImageDraw.Draw(image)
    for x in range(30, width, 65):
        draw.line((x, 15, x, height - 14), fill=(28, 62, 98), width=1)
    for y in range(20, height, 65):
        draw.line((20, y, width - 18, y), fill=(28, 62, 98), width=1)
    for i in range(15):
        x = 670 + i * 26
        h = 32 + i * 37 % 108
        draw.rectangle((x, height - h - 11, x + 19, height - 10), fill=(23, 58, 90))
        for j in range(3):
            for k in range(1, int(h / 20)):
                draw.rectangle((x + 3 + j * 5, height - 13 - k * 15, x + 5 + j * 5, height - 11 - k * 15), fill=(67, 112, 148))
    draw.text((38, 26), 'SaS  /  STATE EVENT CENTER', font=typeface(20, True), fill=(158, 206, 235))
    stage_text = {'upcoming': 'UPCOMING EVENT', '24h': '24 HOURS LEFT', '1h': '1 HOUR LEFT', '15m': '15 MINUTES LEFT', 'start': 'STARTING NOW', 'cancelled': 'EVENT CANCELLED'}.get(stage, 'EVENT UPDATE')
    draw.text((38, 74), stage_text, font=fit_text(draw, stage_text, 650, 52, True), fill=(255, 207, 103))
    draw.text((40, 150), title, font=fit_text(draw, title, 660, 35, True), fill=(250, 252, 255))
    draw.text((40, 224), category.upper() + '  |  SERVER TIME UTC', font=typeface(19, True), fill=(177, 198, 219))
    draw.ellipse((755, 65, 944, 254), outline=(80, 174, 228), width=15)
    draw.ellipse((770, 80, 930, 240), outline=(231, 182, 88), width=6)
    draw.line((850, 161, 850, 104), fill=(245, 245, 248), width=10)
    draw.line((850, 161, 899, 185), fill=(245, 245, 248), width=10)
    draw.ellipse((839, 150, 861, 172), fill=(252, 204, 108))
    draw.line((0, height - 4, width, height - 4), fill=(235, 181, 77), width=4)
    output = BytesIO()
    image.save(output, format='PNG', optimize=True)
    output.seek(0)
    return output
