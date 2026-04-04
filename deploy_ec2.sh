#!/bin/bash
# EC2 setup script — run this after SSHing into a fresh Amazon Linux 2023 instance.
# Assumes the instance has an IAM role attached with Bedrock + S3 access.
# If using env-file credentials instead of role, copy .env manually after cloning.

set -e

echo "=== Installing dependencies ==="
sudo dnf update -y
sudo dnf install -y python3.11 python3.11-pip git

# use python3.11 explicitly — Amazon Linux default python3 is older
python3.11 -m pip install --upgrade pip

echo "=== Cloning repo ==="
# Replace with your actual repo URL
git clone https://github.com/YOUR_USERNAME/enterprise-rag-bedrock.git
cd enterprise-rag-bedrock

echo "=== Installing Python packages ==="
pip3.11 install -r requirements.txt

echo "=== Creating .env from example ==="
# If using IAM role on EC2, you can skip AWS key/secret in .env
# Just set the KB and S3 variables
cp .env.example .env
echo ""
echo "Edit .env with your actual values before running the app:"
echo "  nano .env"
echo ""

echo "=== Configuring Streamlit for EC2 ==="
mkdir -p ~/.streamlit
cat > ~/.streamlit/config.toml << 'EOF'
[server]
port = 8501
address = "0.0.0.0"
headless = true

[browser]
gatherUsageStats = false
EOF

echo "=== Opening firewall port 8501 ==="
# Make sure your EC2 security group allows inbound TCP 8501
# This just reminds you — it doesn't change the security group itself
echo "REMINDER: Add inbound rule for port 8501 in your EC2 Security Group"

echo ""
echo "=== Setup complete ==="
echo "Start the app with:"
echo "  streamlit run app.py"
echo ""
echo "Or run in background:"
echo "  nohup streamlit run app.py > streamlit.log 2>&1 &"
