pipeline {
  agent any
  environment {
    PYTHONUNBUFFERED = '1'
  }
  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }
    stage('Install') {
      steps {
        sh 'python -m pip install --upgrade pip && python -m pip install -e .'
      }
    }
    stage('Performance Gate') {
      steps {
        sh 'python -m perf_agent run --config examples/perf_agent_config.json --out runs --approve-risky --release-gate'
      }
    }
  }
  post {
    always {
      archiveArtifacts artifacts: 'runs/**', fingerprint: true
    }
  }
}
