from .providers import BaseStreamProvider, ProviderUnsupportedError, StreamMetadata


class TwitchProvider(BaseStreamProvider):
    name = "twitch"

    def authenticate(self, request_write_scopes: bool = False) -> dict:
        raise ProviderUnsupportedError("Twitch esta preparado como placeholder para una fase futura")

    def read_metadata(self) -> StreamMetadata:
        return StreamMetadata(
            provider=self.name,
            status="placeholder",
            title="Twitch no disponible en MVP",
            description="Proveedor preparado como placeholder. Implementacion real futura.",
        )
