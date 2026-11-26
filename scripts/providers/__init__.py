"""Official data providers. Each provider returns a pandas DataFrame indexed by
observation date (DatetimeIndex named "date") with one float column per series,
in the native unit published by the source. Providers never fill gaps, never
substitute zeros and never drop negative values; missing observations are simply
absent. Any problem raises providers.http.ProviderError."""
