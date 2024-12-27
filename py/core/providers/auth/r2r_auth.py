import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

from core.base import (
    AuthConfig,
    AuthProvider,
    CollectionResponse,
    CryptoProvider,
    EmailProvider,
    R2RException,
    Token,
    TokenData,
)
from core.base.api.models import User

from ...database.postgres import PostgresDatabaseProvider

DEFAULT_ACCESS_LIFETIME_IN_MINUTES = 3600
DEFAULT_REFRESH_LIFETIME_IN_DAYS = 7

logger = logging.getLogger()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


class R2RAuthProvider(AuthProvider):
    def __init__(
        self,
        config: AuthConfig,
        crypto_provider: CryptoProvider,
        database_provider: PostgresDatabaseProvider,
        email_provider: EmailProvider,
    ):
        super().__init__(
            config, crypto_provider, database_provider, email_provider
        )
        self.database_provider: PostgresDatabaseProvider = database_provider
        logger.debug(f"Initializing R2RAuthProvider with config: {config}")

        # We no longer use a local secret_key or defaults here.
        # All key handling is done in the crypto_provider.
        self.access_token_lifetime_in_minutes = (
            config.access_token_lifetime_in_minutes
            or os.getenv("R2R_ACCESS_LIFE_IN_MINUTES")
            or DEFAULT_ACCESS_LIFETIME_IN_MINUTES
        )
        self.refresh_token_lifetime_in_days = (
            config.refresh_token_lifetime_in_days
            or os.getenv("R2R_REFRESH_LIFE_IN_DAYS")
            or DEFAULT_REFRESH_LIFETIME_IN_DAYS
        )
        self.config: AuthConfig = config

    async def initialize(self):
        try:
            user = await self.register(
                email=self.admin_email,
                password=self.admin_password,
                is_superuser=True,
            )
            await self.database_provider.users_handler.mark_user_as_superuser(
                id=user.id
            )
        except R2RException:
            logger.info("Default admin user already exists.")

    def create_access_token(self, data: dict) -> str:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=float(self.access_token_lifetime_in_minutes)
        )
        # Add token_type and pass data/expiry to crypto_provider
        data_with_type = {**data, "token_type": "access"}
        return self.crypto_provider.generate_secure_token(
            data=data_with_type,
            expiry=expire,
        )

    def create_refresh_token(self, data: dict) -> str:
        expire = datetime.now(timezone.utc) + timedelta(
            days=float(self.refresh_token_lifetime_in_days)
        )
        data_with_type = {**data, "token_type": "refresh"}
        return self.crypto_provider.generate_secure_token(
            data=data_with_type,
            expiry=expire,
        )

    async def decode_token(self, token: str) -> TokenData:
        # First, check if the token is blacklisted
        if await self.database_provider.token_handler.is_token_blacklisted(
            token=token
        ):
            raise R2RException(
                status_code=401, message="Token has been invalidated"
            )

        # Verify token using crypto_provider
        payload = self.crypto_provider.verify_secure_token(token=token)
        if payload is None:
            raise R2RException(
                status_code=401, message="Invalid or expired token"
            )

        email: str = payload.get("sub")
        token_type: str = payload.get("token_type")
        exp: float = payload.get("exp")
        if email is None or token_type is None or exp is None:
            raise R2RException(status_code=401, message="Invalid token claims")

        exp_datetime = datetime.fromtimestamp(exp, tz=timezone.utc)
        if exp_datetime < datetime.now(timezone.utc):
            raise R2RException(status_code=401, message="Token has expired")

        return TokenData(email=email, token_type=token_type, exp=exp_datetime)

    async def authenticate_api_key(self, api_key: str) -> Optional[User]:
        """
        Authenticate using an API key of the form "public_key.raw_key".
        Returns a User if successful, or raises R2RException if not.
        """
        try:
            key_id, raw_key = api_key.split(".", 1)
        except ValueError:
            raise R2RException(
                status_code=401, message="Invalid API key format"
            )

        key_record = (
            await self.database_provider.users_handler.get_api_key_record(
                key_id=key_id
            )
        )
        if not key_record:
            raise R2RException(status_code=401, message="Invalid API key")

        if not self.crypto_provider.verify_api_key(
            raw_api_key=raw_key, hashed_key=key_record["hashed_key"]
        ):
            raise R2RException(status_code=401, message="Invalid API key")

        user = await self.database_provider.users_handler.get_user_by_id(
            id=key_record["user_id"]
        )
        if not user.is_active:
            raise R2RException(
                status_code=401, message="User account is inactive"
            )

        return user

    async def user(self, token: str = Depends(oauth2_scheme)) -> User:
        """
        Attempt to authenticate via JWT first, then fallback to API key.
        """
        # Try JWT auth
        try:
            token_data = await self.decode_token(token=token)
            if not token_data.email:
                raise R2RException(
                    status_code=401, message="Could not validate credentials"
                )
            user = (
                await self.database_provider.users_handler.get_user_by_email(
                    email=token_data.email
                )
            )
            if user is None:
                raise R2RException(
                    status_code=401,
                    message="Invalid authentication credentials",
                )
            return user
        except R2RException:
            # If JWT fails, try API key auth
            # OAuth2PasswordBearer provides token as "Bearer xxx", strip it if needed
            token = token.removeprefix("Bearer ")
            return await self.authenticate_api_key(api_key=token)

    def get_current_active_user(
        self, current_user: User = Depends(user)
    ) -> User:
        if not current_user.is_active:
            raise R2RException(status_code=400, message="Inactive user")
        return current_user

    async def register(
        self, email: str, password: str, is_superuser: bool = False
    ) -> User:
        new_user = await self.database_provider.users_handler.create_user(
            email=email, password=password, is_superuser=is_superuser
        )
        default_collection: CollectionResponse = (
            await self.database_provider.collections_handler.create_collection(
                owner_id=new_user.id,
            )
        )
        graph_result = await self.database_provider.graphs_handler.create(
            collection_id=default_collection.id,
            name=default_collection.name,
            description=default_collection.description,
        )

        await self.database_provider.users_handler.add_user_to_collection(
            new_user.id, default_collection.id
        )

        if self.config.require_email_verification:
            verification_code = (
                self.crypto_provider.generate_verification_code()
            )
            expiry = datetime.now(timezone.utc) + timedelta(hours=24)
            await self.database_provider.users_handler.store_verification_code(
                id=new_user.id,
                verification_code=verification_code,
                expiry=expiry,
            )
            new_user.verification_code_expiry = expiry

            first_name = (
                new_user.name.split(" ")[0]
                if new_user.name
                else email.split("@")[0]
            )

            await self.email_provider.send_verification_email(
                new_user.email, verification_code, {"first_name": first_name}
            )
        else:
            expiry = datetime.now(timezone.utc) + timedelta(hours=366 * 10)
            await self.database_provider.users_handler.store_verification_code(
                id=new_user.id,
                verification_code=str(-1),
                expiry=expiry,
            )
            await self.database_provider.users_handler.mark_user_as_verified(
                id=new_user.id
            )

        return new_user

    async def verify_email(
        self, email: str, verification_code: str
    ) -> dict[str, str]:
        user_id = await self.database_provider.users_handler.get_user_id_by_verification_code(
            verification_code=verification_code
        )
        await self.database_provider.users_handler.mark_user_as_verified(
            id=user_id
        )
        await self.database_provider.users_handler.remove_verification_code(
            verification_code=verification_code
        )
        return {"message": "Email verified successfully"}

    async def login(self, email: str, password: str) -> dict[str, Token]:
        logger.debug(f"Attempting login for email: {email}")
        user = await self.database_provider.users_handler.get_user_by_email(
            email=email
        )

        logger.debug(f"User found: {user}")

        if not isinstance(user.hashed_password, str):
            logger.error(
                f"Invalid hashed_password type: {type(user.hashed_password)}"
            )
            raise HTTPException(
                status_code=500,
                detail="Invalid password hash in database",
            )

        try:
            password_verified = self.crypto_provider.verify_password(
                plain_password=password,
                hashed_password=user.hashed_password,
            )
        except Exception as e:
            logger.error(f"Error during password verification: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail="Error during password verification",
            ) from e

        if not password_verified:
            logger.warning(f"Invalid password for user: {email}")
            raise R2RException(
                status_code=401, message="Incorrect email or password"
            )

        if not user.is_verified and self.config.require_email_verification:
            logger.warning(f"Unverified user attempted login: {email}")
            raise R2RException(status_code=401, message="Email not verified")

        access_token = self.create_access_token(data={"sub": user.email})
        refresh_token = self.create_refresh_token(data={"sub": user.email})
        return {
            "access_token": Token(token=access_token, token_type="access"),
            "refresh_token": Token(token=refresh_token, token_type="refresh"),
        }

    async def refresh_access_token(
        self, refresh_token: str
    ) -> dict[str, Token]:
        token_data = await self.decode_token(refresh_token)
        if token_data.token_type != "refresh":
            raise R2RException(
                status_code=401, message="Invalid refresh token"
            )

        # Invalidate the old refresh token and create a new one
        await self.database_provider.token_handler.blacklist_token(
            token=refresh_token
        )

        new_access_token = self.create_access_token(
            data={"sub": token_data.email}
        )
        new_refresh_token = self.create_refresh_token(
            data={"sub": token_data.email}
        )
        return {
            "access_token": Token(token=new_access_token, token_type="access"),
            "refresh_token": Token(
                token=new_refresh_token, token_type="refresh"
            ),
        }

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> dict[str, str]:
        if not isinstance(user.hashed_password, str):
            logger.error(
                f"Invalid hashed_password type: {type(user.hashed_password)}"
            )
            raise HTTPException(
                status_code=500,
                detail="Invalid password hash in database",
            )

        if not self.crypto_provider.verify_password(
            plain_password=current_password,
            hashed_password=user.hashed_password,
        ):
            raise R2RException(
                status_code=400, message="Incorrect current password"
            )

        hashed_new_password = self.crypto_provider.get_password_hash(
            password=new_password
        )
        await self.database_provider.users_handler.update_user_password(
            id=user.id,
            new_hashed_password=hashed_new_password,
        )
        return {"message": "Password changed successfully"}

    async def request_password_reset(self, email: str) -> dict[str, str]:
        try:

            user = (
                await self.database_provider.users_handler.get_user_by_email(
                    email=email
                )
            )

            reset_token = self.crypto_provider.generate_verification_code()
            expiry = datetime.now(timezone.utc) + timedelta(hours=1)
            await self.database_provider.users_handler.store_reset_token(
                id=user.id,
                reset_token=reset_token,
                expiry=expiry,
            )

            first_name = (
                user.name.split(" ")[0] if user.name else email.split("@")[0]
            )
            await self.email_provider.send_password_reset_email(
                email, reset_token, {"first_name": first_name}
            )

            return {
                "message": "If the email exists, a reset link has been sent"
            }
        except R2RException as e:
            if e.status_code == 404:
                # User doesn't exist; return a success message anyway
                return {
                    "message": "If the email exists, a reset link has been sent"
                }
            else:
                raise

    async def confirm_password_reset(
        self, reset_token: str, new_password: str
    ) -> dict[str, str]:
        user_id = await self.database_provider.users_handler.get_user_id_by_reset_token(
            reset_token=reset_token
        )
        if not user_id:
            raise R2RException(
                status_code=400, message="Invalid or expired reset token"
            )

        hashed_new_password = self.crypto_provider.get_password_hash(
            password=new_password
        )
        await self.database_provider.users_handler.update_user_password(
            id=user_id,
            new_hashed_password=hashed_new_password,
        )
        await self.database_provider.users_handler.remove_reset_token(
            id=user_id
        )
        return {"message": "Password reset successfully"}

    async def logout(self, token: str) -> dict[str, str]:
        await self.database_provider.token_handler.blacklist_token(token=token)
        return {"message": "Logged out successfully"}

    async def clean_expired_blacklisted_tokens(self):
        await self.database_provider.token_handler.clean_expired_blacklisted_tokens()

    async def send_reset_email(self, email: str) -> dict:
        user = await self.database_provider.users_handler.get_user_by_email(
            email=email
        )
        if not user:
            raise R2RException(status_code=404, message="User not found")

        verification_code = self.crypto_provider.generate_verification_code()
        expiry = datetime.now(timezone.utc) + timedelta(hours=24)
        await self.database_provider.users_handler.store_verification_code(
            id=user.id,
            verification_code=verification_code,
            expiry=expiry,
        )

        first_name = (
            user.name.split(" ")[0] if user.name else email.split("@")[0]
        )
        await self.email_provider.send_verification_email(
            email, verification_code, {"first_name": first_name}
        )

        return {
            "verification_code": verification_code,
            "expiry": expiry,
            "message": f"Verification email sent successfully to {email}",
        }

    async def create_user_api_key(
        self, user_id: UUID, name: Optional[str] = None
    ) -> dict[str, str]:
        key_id, raw_api_key = self.crypto_provider.generate_api_key()
        hashed_key = self.crypto_provider.hash_api_key(raw_api_key)

        api_key_uuid = (
            await self.database_provider.users_handler.store_user_api_key(
                user_id=user_id,
                key_id=key_id,
                hashed_key=hashed_key,
                name=name,
            )
        )

        return {
            "api_key": f"{key_id}.{raw_api_key}",
            "key_id": str(api_key_uuid),
            "public_key": key_id,
            "name": name or "",
        }

    async def list_user_api_keys(self, user_id: UUID) -> list[dict]:
        return await self.database_provider.users_handler.get_user_api_keys(
            user_id=user_id
        )

    async def delete_user_api_key(self, user_id: UUID, key_id: UUID) -> bool:
        return await self.database_provider.users_handler.delete_api_key(
            user_id=user_id,
            key_id=key_id,
        )

    async def rename_api_key(
        self, user_id: UUID, key_id: UUID, new_name: str
    ) -> bool:
        return await self.database_provider.users_handler.update_api_key_name(
            user_id=user_id,
            key_id=key_id,
            name=new_name,
        )