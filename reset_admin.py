from werkzeug.security import generate_password_hash

print(
    generate_password_hash(
        "123456",
        method="scrypt"
    )
)