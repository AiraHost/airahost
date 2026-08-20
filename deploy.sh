#!/bin/bash
set -euo pipefail # Exit immediately if a command exits with a non-zero status.
                  # -u: Treat unset variables as an error.
                  # -o pipefail: The return value of a pipeline is the status of the last command to exit with a non-zero status, or zero if all commands exit successfully.

# These variables will be passed from the GitHub Actions workflow
# Example: GITHUB_REPOSITORY="your-org/your-repo"
# Example: GITHUB_SHA="abcdef1234567890abcdef1234567890abcdef12"
# Example: SUPABASE_URL="https://your-project.supabase.co"
# Example: SUPABASE_SERVICE_ROLE_KEY="your-service-role-key"
# Example: CDP_URL="http://127.0.0.1:9222"

IMAGE_NAME="ghcr.io/${GITHUB_REPOSITORY,,}/worker"
IMAGE_TAG="${GITHUB_SHA}"
CONTAINER_NAME="airahost-worker"

echo "--- Starting Deployment of Worker ---"
echo "Image: ${IMAGE_NAME}:${IMAGE_TAG}"
echo "Container Name: ${CONTAINER_NAME}"

# Log in to GHCR (if token is provided)
if [ -n "$GITHUB_TOKEN" ]; then
  echo "${GITHUB_TOKEN}" | docker login ghcr.io -u "${GITHUB_ACTOR}" --password-stdin
fi

# Pull the new image
echo "Pulling new image..."
docker pull "${IMAGE_NAME}:${IMAGE_TAG}"

# Stop and remove the old container (if it exists)
echo "Stopping and removing old container (if any)..."
docker stop "${CONTAINER_NAME}" || true # `|| true` prevents script from failing if container doesn't exist
docker rm "${CONTAINER_NAME}" || true

# Run the new container
echo "Starting new container..."
docker run -d \
-p 8000:8000 \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -e "SUPABASE_URL=${SUPABASE_URL}" \
  -e "SUPABASE_SERVICE_ROLE_KEY=${SUPABASE_SERVICE_ROLE_KEY}" \
  -e "CDP_URL=${CDP_URL}" \
  -e "WORKER_VERSION=${GITHUB_SHA}" \
  "${IMAGE_NAME}:${IMAGE_TAG}"

echo "Deployment initiated. Giving container 5 seconds to start..."
sleep 5 # Give the container a moment to start

if docker ps -f "name=${CONTAINER_NAME}" --format "{{.ID}}" | grep -q .; then
  echo "Container ${CONTAINER_NAME} started successfully."
else
  echo "Error: Container ${CONTAINER_NAME} failed to start. Checking logs..."
  docker logs "${CONTAINER_NAME}" # Show logs for debugging
  exit 1
fi

echo "--- Deployment Complete ---"