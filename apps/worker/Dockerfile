# Build context: the repository root.  docker build -f apps/worker/Dockerfile .
FROM mcr.microsoft.com/dotnet/sdk:10.0.302-noble@sha256:72dd743782f2ae7e5476fd64f6a460045e3998dc862218b80e6944cba79a01b0 AS build
WORKDIR /src
COPY .editorconfig global.json Directory.Build.props Directory.Packages.props ./
COPY apps/worker/Worker.csproj apps/worker/packages.lock.json apps/worker/
RUN dotnet restore apps/worker/Worker.csproj --locked-mode
COPY apps/worker/ apps/worker/
RUN dotnet publish apps/worker/Worker.csproj -c Release --no-restore -o /app

FROM mcr.microsoft.com/dotnet/aspnet:10.0.9-noble@sha256:7644f992230d35cf230017189d4038c0ae0f7388b13f4f7ae1900a155bafb597
# curl only for the compose healthcheck: curl -fsS http://127.0.0.1:8080/healthz
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app .
USER $APP_UID
EXPOSE 8080
ENTRYPOINT ["dotnet", "Worker.dll"]
