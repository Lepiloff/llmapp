#!/bin/bash

# 🚀 LLM App Market - Quick Start Script

set -e

echo "🚀 Starting LLM App Market..."

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null; then
    echo "❌ docker-compose is not installed. Please install Docker and Docker Compose."
    exit 1
fi

# Check if .env file exists
if [ ! -f .env ]; then
    echo "📋 Creating .env file..."
    cp .env.template .env 2>/dev/null || cp .env.example .env 2>/dev/null || echo "⚠️  .env file not found, using defaults"
fi

# Build and start services
echo "🔧 Building Docker images..."
docker-compose build --no-cache

echo "🚀 Starting services..."
docker-compose up -d

# Wait for services to be ready
echo "⏳ Waiting for services to start..."
sleep 15

# Check if services are healthy
echo "🩺 Checking service health..."
if docker-compose ps | grep -q "healthy\|Up"; then
    echo "✅ Services are running!"
else
    echo "⚠️  Some services may not be ready yet. Check logs with: docker-compose logs"
fi

echo ""
echo "🎉 LLM App Market is starting!"
echo ""
echo "📱 Web Application:  http://localhost:8000"
echo "👤 Django Admin:     http://localhost:8000/admin"
echo "   Username: admin"
echo "   Password: admin123"
echo ""
echo "📊 Useful commands:"
echo "   docker-compose logs -f    # View logs"
echo "   docker-compose down       # Stop services"
echo "   make docker-ingest        # Load sample data"
echo ""
echo "📖 Check README.md for more information"