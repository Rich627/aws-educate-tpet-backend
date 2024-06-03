import re
import datetime
import io
import json
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import boto3
from botocore.exceptions import ClientError
import pandas as pd
import uuid

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Get the template from S3
def get_template(template_file_id):
    try:
        s3 = boto3.client("s3")
        bucket_name = os.environ.get("BUCKET_NAME")
        request = s3.get_object(Bucket=bucket_name, Key=template_file_id)
        template_content = request['Body'].read().decode("utf-8")
        return template_content
    except Exception as e:
        print(f"Error in get_template: {str(e)}")
        raise

# Get the spreadsheet and read the data
def read_sheet_data_from_s3(spreadsheet_file_id):
    try:
        s3 = boto3.client("s3")
        bucket_name = os.environ.get("BUCKET_NAME")

        request = s3.get_object(Bucket=bucket_name, Key=spreadsheet_file_id)
        xlsx_content = request['Body'].read()
        excel_data = pd.read_excel(io.BytesIO(xlsx_content), engine='openpyxl')

        rows = excel_data.to_dict(orient='records')

        if excel_data.empty:
            return [], 0, True

        return rows, excel_data.columns.tolist()
    except Exception as e:
        print(f"Error in read_sheet_data_from_s3: {str(e)}")
        raise

# Check if the template has all the required columns
def validate_template(template_content, columns):
    try:
        placeholders = re.findall(r'{{(.*?)}}', template_content)
        missing_columns = [placeholder for placeholder in placeholders if placeholder not in columns]
        return missing_columns
    except Exception as e:
        print(f"Error in validate_template: {str(e)}")
        raise

def send_email(ses_client, email_title, template_content, row, display_name):
    try:
        template_content = template_content.replace("\r", "")

        # Convert {{Name}} to {Name} for proper formatting
        template_content = re.sub(r'\{\{(.*?)\}\}', r'{\1}', template_content)

        receiver_email = row.get("Email")
        if not receiver_email:
            print(f"No email address provided for {row.get('Name', 'Unknown')}. Skipping...")
            return "Failed", "FAILED"

        try:
            formatted_content = template_content.format(**row)
            source_email = "awseducate.cloudambassador@gmail.com"
            formatted_source_email = f"{display_name} <{source_email}>"
            ses_client.send_email(
                Source=formatted_source_email,
                Destination={"ToAddresses": [receiver_email]},
                Message={
                    "Subject": {"Data": email_title},
                    "Body": {
                        "Html": {
                            "Data": formatted_content
                        }
                    },
                },
            )
            _ = datetime.datetime.now() + datetime.timedelta(hours=8)
            formatted_send_time = _.strftime(TIME_FORMAT)
            print(f"Email sent to {row.get('Name', 'Unknown')} at {formatted_send_time}")
            return formatted_send_time, "SUCCESS"
        except Exception as e:
            print(f"Failed to send email to {row.get('Name', 'Unknown')}: {e}")
            return "Failed", "FAILED"
    except Exception as e:
        print(f"Error in send_email: {str(e)}")
        raise

def save_to_dynamodb(run_id, email_id, display_name, status, recipient_email, template_file_id, spreadsheet_file_id, created_at):
    try:
        dynamodb = boto3.resource('dynamodb')
        table_name = os.environ.get('DYNAMODB_TABLE')
        table = dynamodb.Table(table_name)

        item = {
            'run_id': run_id,
            'email_id': email_id,
            'display_name': display_name,
            'status': status,
            'recipient_email': recipient_email,
            'template_file_id': template_file_id,
            'spreadsheet_file_id': spreadsheet_file_id,
            'created_at': created_at
        }
        try:
            table.put_item(Item=item)
            print(f"Saved record to DynamoDB: {item}")
        except ClientError as e:
            print(f"Error saving to DynamoDB: {e}")
    except Exception as e:
        print(f"Error in save_to_dynamodb: {str(e)}")
        raise

def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body", "{}"))
        template_file_id = body.get("template_file_id")
        spreadsheet_id = body.get("spreadsheet_file_id")
        email_title = body.get("subject")
        display_name = body.get("display_name")  
        run_id = body.get("run_id") if body.get("run_id") else uuid.uuid4().hex

        if not template_file_id or not spreadsheet_id or not email_title or not display_name:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    "Missing template_file_id, spreadsheet_id, email_title, or display_name"
                ),
            }

        template_content = get_template(template_file_id)
        data, columns = read_sheet_data_from_s3(spreadsheet_id)

        missing_columns = validate_template(template_content, columns)
        if missing_columns:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    f"Missing columns in Excel for placeholders: {', '.join(missing_columns)}"
                ),
            }

        ses_client = boto3.client("ses", region_name="ap-northeast-1")
        failed_recipients = []
        for row in data:
            _ , status = send_email(ses_client, email_title, template_content, row, display_name)
            if status == "FAILED":
                failed_recipients.append(row.get("Email"))
            save_to_dynamodb(
                run_id,
                email_id=uuid.uuid4().hex,  
                display_name=display_name,
                status=status,
                recipient_email=row.get("Email"),
                template_file_id=template_file_id,
                spreadsheet_file_id=spreadsheet_id,
                created_at=datetime.datetime.now().strftime(TIME_FORMAT)
            )

        response = {
            "status": "success",
            "message": "Email request has been queued successfully.",
            "request_id": run_id,
            "timestamp": datetime.datetime.now().strftime(TIME_FORMAT),
            "sqs_message_id": uuid.uuid4().hex
        }

        if failed_recipients:
            response["failed_recipients"] = failed_recipients
            return {"statusCode": 207, "body": json.dumps(response)}
        else:
            return {"statusCode": 200, "body": json.dumps(response)}

    except Exception as e:
        print(f"Error in lambda_handler: {str(e)}")
        return {"statusCode": 500, "body": json.dumps(f"Internal server error: {str(e)}")}
