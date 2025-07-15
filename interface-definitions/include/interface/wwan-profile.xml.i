<!-- include start from interface/dhcp-wwan-profile.xml.i -->
<node name="wwan-profile">
  <properties>
    <help>WWAN profile settings</help>
    <valueHelp>
      <format>string</format>
      <description>Profile name for the WWAN connection</description>
    </valueHelp>
    <constraint>
      <regex>(primary|alternate)</regex>
    </constraint>
  </properties>
  <children>
    <leafNode name="apn">
        <properties>
        <help>Access Point Name (APN)</help>
        <valueHelp>
            <format>string</format>
            <description>APN string for the WWAN connection</description>
        </valueHelp>
        </properties>
    </leafNode>

    <node name="wwan-authentication">
        <properties>
          <help>Authentication method for the APN</help>
          <valueHelp>
              <format>string</format>
              <description>Authentication method for the WWAN connection</description>
          </valueHelp>
          <constraint>
              <regex>(chap|pap)</regex>
          </constraint>
        </properties>
        <children>
          <leafNode name="wwan-username">
              <properties>
                <help>Username for APN authentication</help>
                <valueHelp>
                    <format>string</format>
                    <description>Username for the WWAN connection</description>
                </valueHelp>
              </properties>
          </leafNode>
          <leafNode name="wwan-password">
              <properties>
                <help>Password for APN authentication</help>
                <valueHelp>
                    <format>string</format>
                    <description>Password for the WWAN connection</description>
                </valueHelp>
              </properties>
          </leafNode>
        </children>
    </node>

    <node name="technology">
        <properties>
          <help>WWAN technology type</help>
          <valueHelp>
              <format>string</format>
              <description>Technology type for the WWAN connection</description>
          </valueHelp>
          <constraint>
              <regex>(5g|lte|wcdma|gsm)</regex>
          </constraint>
        </properties>
        <children>
          <leafNode name="band">
              <properties>
                <help>WWAN band for the technology</help>
                <valueHelp>
                    <format>string</format>
                    <description>Band for the WWAN technology</description>
                </valueHelp>
              </properties>
          </leafNode>
        </children>
    </node>

    <leafNode name="cid">
        <properties>
          <help>Context ID for the WWAN profile</help>
          <valueHelp>
              <format>u32:0-65535</format>
              <description>Context ID for the WWAN connection</description>
          </valueHelp>
          <constraint>
              <validator name="numeric" argument="--range 0-65535"/>
          </constraint>
        </properties>
    </leafNode>

    <leafNode name="pdp-type">
        <properties>
          <help>PDP type for the WWAN profile</help>
          <valueHelp>
              <format>string</format>
              <description>PDP type for the WWAN connection</description>
          </valueHelp>
          <constraint>
              <regex>(ipv4|ipv6|ipv4v6)</regex>
          </constraint>
        </properties>
    </leafNode>

    <leafNode name="roaming">
        <properties>
          <help>Roaming settings for the WWAN profile</help>
          <valueHelp>
              <format>boolean</format>
              <description>Enable or disable roaming for the WWAN connection</description>
          </valueHelp>
        </properties>
        <defaultValue>false</defaultValue>
    </leafNode>

    <leafNode name="sim-slot">
        <properties>
          <help>SIM slot for the WWAN profile</help>
          <valueHelp>
              <format>u32:0-2</format>
              <description>SIM slot for the WWAN connection</description>
          </valueHelp>
          <constraint>
              <validator name="numeric" argument="--range 0-2"/>
          </constraint>
        </properties>
    </leafNode>

    <node name="wwan-data-limit">
      <properties>
        <help>Data limit for the WWAN profile</help>
      </properties>
      <children>
        <leafNode name="action">
          <properties>
            <help>Action when data limit is reached</help>
            <valueHelp>
              <format>string</format>
              <description>Action to take when data limit is reached</description>
            </valueHelp>
            <constraint>
              <regex>(block|notify)</regex>
            </constraint>
          </properties>
        </leafNode>
        <leafNode name="alert-on-percent-used">
          <properties>
            <help>Alert when a percentage of data limit is used</help>
            <valueHelp>
              <format>u32:0-100</format>
              <description>Percentage of data limit used to trigger an alert</description>
            </valueHelp>
            <constraint>
              <validator name="numeric" argument="--range 0-100"/>
            </constraint>
          </properties>
        </leafNode>
        <leafNode name="alert-method">
          <properties>
            <help>Alert method when data limit is reached</help>
            <valueHelp>
              <format>string</format>
              <description>Method to alert when data limit is reached</description>
            </valueHelp>
            <constraint>
              <regex>(syslog|snmp|relay)</regex>
            </constraint>
          </properties>
        </leafNode>
        <leafNode name="bill-day">
          <properties>
            <help>Billing day for the data limit</help>
            <valueHelp>
              <format>u32:1-31</format>
              <description>Day of the month when billing starts</description>
            </valueHelp>
            <constraint>
              <validator name="numeric" argument="--range 1-31"/>
            </constraint>
          </properties>
        </leafNode>
        <leafNode name="mb-limit">
          <properties>
            <help>Data limit in megabytes</help>
            <valueHelp>
              <format>u32:1-4294967295</format>
              <description>Data limit in megabytes for the WWAN connection</description>
            </valueHelp>
            <constraint>
              <validator name="numeric" argument="--range 1-4294967295"/>
            </constraint>
          </properties>
        </leafNode>
      </children>
    </node>
</children>
</node>
<!-- include end -->
