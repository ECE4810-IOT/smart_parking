"""Security layer.

Design principle: use vetted libraries (`cryptography`, `hashlib`, `hmac`,
`secrets`) for every cryptographic primitive - "don't roll your own crypto" -
and spend the engineering effort on choosing and combining them correctly.
The only hand-written cipher is the digit-Vigenere OTP layer (Lecture 8c),
which is secure here because every OTP is fresh and uniformly random.
"""
