from html import escape

IMAGE_CID = "ig_product_image"


def greeting_name(name: str) -> str:
    cleaned = (name or "").strip()
    return cleaned or "there"


def get_outreach_subject(name: str) -> str:
    return "✨✨𝑰𝑮 𝒑𝒂𝒊𝒅 𝒄𝒐𝒍𝒍𝒂𝒃𝒐𝒓𝒂𝒕𝒊𝒐𝒏 – 𝒛𝒅𝒆𝒆𝒓 𝒗𝒊𝒓𝒂𝒍 𝒆𝒍𝒆𝒄𝒕𝒓𝒐𝒏𝒊𝒄 𝒎𝒐𝒖𝒕𝒉 𝒔𝒑𝒓𝒂𝒚"


def get_outreach_plain_body(name: str, ig_url: str) -> str:
    creator_name = greeting_name(name)
    ig_line = f"\nI came across your Instagram here: {ig_url}\n" if ig_url else ""
    return f"""Hi {creator_name},

Hope you've been doing well! 😊
I'm Eloise from the zdeer team.

I recently came across your content and felt that your style could be a great fit for our zdeer Smart Oral Freshener Spray, which we're currently promoting across both TikTok and Instagram.{ig_line}

One of the reasons we're excited about this product is because of how consistently it performs with creators. We've seen numerous videos generate millions—and even tens of millions—of views on TikTok, and this product has already surpassed 100,000 orders.

If you'd like to learn more about the product, you can check it out here:
https://zdeer.com/products/zdeer-electric-oral-spray#looxReviewsFrame

We're currently looking for creators for a paid Instagram collaboration and would love to explore working with you.

For this opportunity, we'd love to offer:
✨ Fixed payment + 10% commission ($3.4 per order)
🎁 Free product sample (valued at $34.99) for content creation
📈 $1,000+ advertising support to help grow your reach and audience, higher commission tiers, and upcoming product launches

We believe creators produce their best content when they have creative freedom, so we're always happy to collaborate on ideas while giving you the flexibility to create content in a way that feels natural to your audience. If helpful, I'd also be happy to share content ideas, creative angles, or even a sample script as a starting point.

Looking forward to hearing from you!

Best,
Eloise
"""


def get_outreach_html_body(name: str, ig_url: str) -> str:
    creator_name = escape(greeting_name(name))
    ig_line = (
        f'<p>I came across your Instagram here: <a href="{escape(ig_url)}">{escape(ig_url)}</a></p>'
        if ig_url
        else ""
    )
    return f"""<!doctype html>
<html>
  <body style="font-family: Arial, Helvetica, sans-serif; font-size: 15px; line-height: 1.55; color: #222;">
    <p>Hi {creator_name},</p>

    <p>Hope you've been doing well! 😊<br>
    I'm Eloise from the zdeer team.</p>

    <p>I recently came across your content and felt that your style could be a great fit for our zdeer Smart Oral Freshener Spray, which we're currently promoting across both TikTok and Instagram.</p>
    {ig_line}

    <p>One of the reasons we're excited about this product is because of how consistently it performs with creators. We've seen numerous videos generate millions—and even tens of millions—of views on TikTok, and this product has already surpassed 100,000 orders.</p>

    <p><img src="cid:{IMAGE_CID}" alt="zdeer creator performance" style="display:block; max-width: 100%; width: 620px; height: auto; border: 0;"></p>

    <p>If you'd like to learn more about the product, you can check it out here:<br>
    <a href="https://zdeer.com/products/zdeer-electric-oral-spray#looxReviewsFrame">https://zdeer.com/products/zdeer-electric-oral-spray#looxReviewsFrame</a></p>

    <p>We're currently looking for creators for a paid Instagram collaboration and would love to explore working with you.</p>

    <p>For this opportunity, we'd love to offer:<br>
    ✨ Fixed payment + 10% commission ($3.4 per order)<br>
    🎁 Free product sample (valued at $34.99) for content creation<br>
    📈 $1,000+ advertising support to help grow your reach and audience, higher commission tiers, and upcoming product launches</p>

    <p>We believe creators produce their best content when they have creative freedom, so we're always happy to collaborate on ideas while giving you the flexibility to create content in a way that feels natural to your audience. If helpful, I'd also be happy to share content ideas, creative angles, or even a sample script as a starting point.</p>

    <p>Looking forward to hearing from you!</p>

    <p>Best,<br>
    Eloise</p>
  </body>
</html>
"""


def get_followup_subject(name: str) -> str:
    return f"Following Up | zdeer x {greeting_name(name)} Instagram Collaboration"


def get_followup_body(name: str, ig_url: str) -> str:
    creator_name = greeting_name(name)
    ig_note = f" I really think your Instagram content ({ig_url}) could be a strong fit for this product." if ig_url else ""
    return f"""Hi {creator_name},

I just wanted to follow up on my previous note about a paid Instagram collaboration with zdeer.{ig_note}

We'd love to offer a fixed payment + 10% commission, a free product sample, and ad support to help amplify the content after it goes live.

If you're interested, feel free to send over your rates and we can discuss the details.

Best,
Eloise
"""
