pipeline {
  agent any
  environment {
    PYTHONUNBUFFERED = '1'
    PERF_AGENT_CONFIG = 'examples/ci_release_gate_config.json'
  }
  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }
    stage('Install') {
      steps {
        sh 'python -m pip install -e .'
      }
    }
    stage('Performance Gate') {
      steps {
        script {
          int gateCode = sh(
            script: 'python -m perf_agent run --config "$PERF_AGENT_CONFIG" --out runs --release-gate',
            returnStatus: true
          )
          env.PERF_GATE_EXIT_CODE = "${gateCode}"
          if (gateCode == 1) {
            unstable('Performance release gate is AMBER. Review archived readiness artifacts.')
          } else if (gateCode >= 2) {
            error('Performance release gate blocked the release.')
          }
        }
      }
    }
  }
  post {
    always {
      archiveArtifacts artifacts: 'runs/**', fingerprint: true, allowEmptyArchive: false
    }
  }
}
