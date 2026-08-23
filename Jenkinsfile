// Sample Jenkinsfile. Kept alongside the GitHub Actions promotion pipeline so
// both CI systems appear in the repository, as in the real project.
pipeline {
    agent any

    environment {
        SAMPLE_VERSION = '1.0.0'
    }

    stages {
        stage('Install') {
            steps {
                sh 'python -m pip install -r requirements.txt -r test_dependencies.txt'
            }
        }
        stage('Test') {
            steps {
                sh 'python -m pytest test'
            }
        }
    }

    post {
        always {
            echo "Sample build ${env.SAMPLE_VERSION} finished"
        }
    }
}
