# Use Alpine-based Python image
FROM python:3.13-alpine

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py \
    FLASK_ENV=production \
    TERRAFORM_VERSION=1.8.2 \
    KUBECTL_VERSION=1.29.3

# Install system dependencies
RUN apk update && \
    apk add --no-cache \
    curl \
    unzip \
    git \
    ca-certificates \
    libc6-compat

# Install Terraform
RUN curl -LO https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip \
    && unzip terraform_${TERRAFORM_VERSION}_linux_amd64.zip \
    && mv terraform /usr/local/bin/ \
    && rm terraform_${TERRAFORM_VERSION}_linux_amd64.zip

# Install kubectl (Alpine-compatible)
RUN curl -LO "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/ \
    && kubectl version --client
# Removed --short flag

# Create non-root user and app directory
RUN adduser -D -g '' appuser && \
    mkdir -p /home/appuser/.kube && \
    chown -R appuser:appuser /home/appuser/.kube

#Work directory
WORKDIR /app

# Install Python dependencies with build dependencies cleanup
COPY requirements.txt .
RUN apk add --no-cache --virtual .build-deps gcc musl-dev python3-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

# Copy application code
COPY . .

# Set permissions
RUN mkdir -p /app/sessions \
    && chown -R appuser:appuser /app \
    && chmod +x /usr/local/bin/terraform \
    && chmod 755 /home/appuser/.kube

# Create session directory with proper permissions
RUN mkdir -p /app/sessions && \
    chown -R appuser:appuser /app/sessions && \
    chmod 755 /app/sessions
    
# Switch to non-root user
USER appuser

# Expose port and healthcheck
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# Run application
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "app:app"]