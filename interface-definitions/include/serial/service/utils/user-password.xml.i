<!-- include start from serial/service/utils/user-password.xml.i -->
<leafNode name="username">
  <properties>
    <help>Username</help>
    <constraint>
      <regex>.{0,255}</regex>
    </constraint>
    <constraintErrorMessage>Username too long (limit 255 characters)</constraintErrorMessage>
  </properties>
</leafNode>
<leafNode name="password">
  <properties>
    <help>Password</help>
    <constraint>
      <regex>.{0,17}</regex>
    </constraint>
    <constraintErrorMessage>Password too long (limit 17 characters)</constraintErrorMessage>
  </properties>
</leafNode>
<leafNode name="remote-user">
  <properties>
    <help>Remote username</help>
    <constraint>
      <regex>.{0,255}</regex>
    </constraint>
    <constraintErrorMessage>Remote username too long (limit 255 characters)</constraintErrorMessage>
  </properties>
</leafNode>
<leafNode name="remote-password">
  <properties>
    <help>Remote password</help>
    <constraint>
      <regex>.{0,17}</regex>
    </constraint>
    <constraintErrorMessage>Remote password too long (limit 17 characters)</constraintErrorMessage>
  </properties>
</leafNode>
<!-- include end -->
